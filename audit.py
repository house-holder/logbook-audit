import csv
import re
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SIM_AIRCRAFT_RE = re.compile(r"^(UAA\s*SIM\b|SIM\d+\b|AATD\b|FTD\b)")


def _to_float(s: str) -> float:
    s = (s or "").strip()
    if not s:
        return 0.0
    try:
        return float(s)
    except ValueError:
        # Sometimes values can be like "0" or "0.00" already handled above; if not, treat as 0.
        return 0.0


def _norm(s: str) -> str:
    return (s or "").strip().upper()


def _is_sim_aircraft_id(aircraft_id: str) -> bool:
    s = _norm(aircraft_id)
    if not s:
        return False
    if SIM_AIRCRAFT_RE.match(s):
        return True
    if "REDBIRD" in s:
        return True
    return False


def _read_foreflight_flights(path: Path) -> List[Dict[str, str]]:
    """
    ForeFlight export is a multi-table CSV. We find the flights header row and then
    parse all rows whose first column looks like YYYY-MM-DD.
    """
    rows: List[List[str]] = []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        for r in reader:
            rows.append(r)

    header: Optional[List[str]] = None
    start_idx = None
    for i, r in enumerate(rows):
        if len(r) >= 3 and r[0].strip() == "Date" and r[1].strip() == "AircraftID":
            header = [c.strip() for c in r]
            start_idx = i + 1
            break

    if not header or start_idx is None:
        raise RuntimeError("Could not find ForeFlight Flights Table header (row starting with 'Date,AircraftID,...').")

    flights: List[Dict[str, str]] = []
    for r in rows[start_idx:]:
        if not r:
            continue
        d = r[0].strip()
        if not DATE_RE.match(d):
            continue
        rec: Dict[str, str] = {}
        for j, name in enumerate(header):
            rec[name] = r[j].strip() if j < len(r) else ""
        flights.append(rec)

    return flights


def _read_logten_flights(path: Path) -> List[Dict[str, str]]:
    """
    LogTen export is TSV with a very wide header. It can contain quoted fields with embedded newlines,
    so we must use csv.reader (not line-by-line splitting).
    """
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f, delimiter="\t", quotechar='"')
        try:
            header_raw = next(reader)
        except StopIteration:
            return []

        header = [h.strip() for h in header_raw]
        flights: List[Dict[str, str]] = []
        for r in reader:
            if not r or (len(r) == 1 and not r[0].strip()):
                continue
            rec: Dict[str, str] = {}
            for j, name in enumerate(header):
                rec[name] = r[j] if j < len(r) else ""
            # Only keep "real" flight rows: must have a date.
            if DATE_RE.match(rec.get("flight_flightDate", "").strip()):
                flights.append(rec)
        return flights


@dataclass(frozen=True)
class FlightKey:
    date: str
    aircraft: str


def _group_by_date_aircraft(
    flights: Iterable[Dict[str, str]],
    date_field: str,
    aircraft_field: str,
) -> Dict[FlightKey, List[Dict[str, str]]]:
    grouped: Dict[FlightKey, List[Dict[str, str]]] = defaultdict(list)
    for f in flights:
        date = f.get(date_field, "").strip()
        ac = _norm(f.get(aircraft_field, ""))
        if not DATE_RE.match(date) or not ac:
            continue
        grouped[FlightKey(date=date, aircraft=ac)].append(f)
    return grouped


def _score_match(ff: Dict[str, str], lt: Dict[str, str]) -> Tuple[float, float, float]:
    """
    Lower is better. Returned tuple sorts lexicographically:
    - from/to mismatch count (0,1,2)
    - abs(total time difference)
    - abs(distance difference) if present
    """
    ff_from = _norm(ff.get("From", ""))
    ff_to = _norm(ff.get("To", ""))
    lt_from = _norm(lt.get(" flight_from", lt.get("flight_from", "")))
    lt_to = _norm(lt.get(" flight_to", lt.get("flight_to", "")))

    mismatch = 0
    if ff_from and lt_from and ff_from != lt_from:
        mismatch += 1
    if ff_to and lt_to and ff_to != lt_to:
        mismatch += 1

    ff_tt = _to_float(ff.get("TotalTime", ""))
    lt_tt = _to_float(lt.get(" flight_totalTime", lt.get("flight_totalTime", "")))

    ff_dist = _to_float(ff.get("Distance", ""))
    lt_dist = _to_float(lt.get(" flight_distance", lt.get("flight_distance", "")))
    dist_diff = abs(ff_dist - lt_dist) if (ff_dist or lt_dist) else 0.0

    return (float(mismatch), abs(ff_tt - lt_tt), dist_diff)


def _match_flights_greedy(
    ff_group: List[Dict[str, str]],
    lt_group: List[Dict[str, str]],
) -> List[Tuple[Dict[str, str], Optional[Dict[str, str]]]]:
    """
    Greedy matching within a (date, aircraft) bucket.
    For each ForeFlight row, pick the best remaining LogTen candidate by score.
    """
    remaining = lt_group[:]
    pairs: List[Tuple[Dict[str, str], Optional[Dict[str, str]]]] = []

    # Process longer flights first to reduce ambiguous "pattern flights" (e.g., 0.7 repeats).
    def ff_sort_key(r: Dict[str, str]) -> float:
        return -_to_float(r.get("TotalTime", ""))

    for ff in sorted(ff_group, key=ff_sort_key):
        if not remaining:
            pairs.append((ff, None))
            continue
        best_idx = None
        best_score = None
        for i, lt in enumerate(remaining):
            sc = _score_match(ff, lt)
            if best_score is None or sc < best_score:
                best_score = sc
                best_idx = i
        if best_idx is None:
            pairs.append((ff, None))
        else:
            lt = remaining.pop(best_idx)
            pairs.append((ff, lt))

    return pairs


def _sum_field(flights: Iterable[Dict[str, str]], field: str) -> float:
    return sum(_to_float(f.get(field, "")) for f in flights)


def _pick_best_custom_time_columns(
    lt_flights: List[Dict[str, str]],
    targets: Dict[str, float],
) -> Dict[str, str]:
    """
    Given target totals (e.g., {"135XC": 675.1, "ATPXC": 342.4}), find which LogTen
    flight_customTimeX columns match those totals best.
    """
    custom_cols = [f"flight_customTime{i}" for i in range(1, 21)]
    col_sums = {c: _sum_field(lt_flights, c) for c in custom_cols}

    remaining = set(custom_cols)
    mapping: Dict[str, str] = {}
    for label, target_total in sorted(targets.items(), key=lambda kv: -kv[1]):
        # NightXC is usually only populated on flights that are BOTH night AND cross-country.
        # Restricting the sum to those flights makes the "match by totals" much less ambiguous.
        if label == "NightXC":
            filtered = [
                r
                for r in lt_flights
                if _to_float(_lt_field(r, "flight_night")) > 0.0 and _to_float(_lt_field(r, "flight_crossCountry")) > 0.0
            ]
            col_sums = {c: _sum_field(filtered, c) for c in remaining}
        best_col = None
        best_diff = None
        for c in remaining:
            diff = abs(col_sums[c] - target_total)
            if best_diff is None or diff < best_diff:
                best_diff = diff
                best_col = c
        if best_col is not None:
            mapping[label] = best_col
            remaining.remove(best_col)
    return mapping


def _lt_field(rec: Dict[str, str], name: str) -> str:
    # Some headers in the file have leading spaces (as seen in the first line).
    return rec.get(name, rec.get(f" {name}", ""))


def _extract_hours_foreflight(ff: Dict[str, str]) -> Dict[str, float]:
    return {
        "TotalTime": _to_float(ff.get("TotalTime", "")),
        "PIC": _to_float(ff.get("PIC", "")),
        "SIC": _to_float(ff.get("SIC", "")),
        "Night": _to_float(ff.get("Night", "")),
        "Solo": _to_float(ff.get("Solo", "")),
        "CrossCountry": _to_float(ff.get("CrossCountry", "")),
        "ActualInstrument": _to_float(ff.get("ActualInstrument", "")),
        "SimulatedInstrument": _to_float(ff.get("SimulatedInstrument", "")),
        "DualGiven": _to_float(ff.get("DualGiven", "")),
        "DualReceived": _to_float(ff.get("DualReceived", "")),
        "Simulator": _to_float(ff.get("SimulatedFlight", "")),
        "135XC": _to_float(ff.get("[Hours]135 XC", "")),
        "ATPXC": _to_float(ff.get("[Hours]ATPXC", "")),
        "NightXC": _to_float(ff.get("[Hours]Night XC", "")),
    }


def _extract_hours_logten(
    lt: Dict[str, str],
    custom_map: Dict[str, str],
    derived_night_xc: bool,
) -> Dict[str, float]:
    total = _to_float(_lt_field(lt, "flight_totalTime"))
    night = _to_float(_lt_field(lt, "flight_night"))
    xc = _to_float(_lt_field(lt, "flight_crossCountry"))

    night_xc_val = _to_float(lt.get(custom_map.get("NightXC", ""), "")) if custom_map.get("NightXC") else 0.0
    if derived_night_xc:
        # LogTen often displays "Night XC" as a derived field. If this leg is logged as XC at all,
        # treat any logged night time as Night XC (user convention: XC is all-or-nothing per leg).
        night_xc_val = night if xc > 0.0 else 0.0

    return {
        "TotalTime": total,
        "PIC": _to_float(_lt_field(lt, "flight_pic")),
        "SIC": _to_float(_lt_field(lt, "flight_sic")),
        "Night": night,
        "Solo": _to_float(_lt_field(lt, "flight_solo")),
        "CrossCountry": xc,
        "ActualInstrument": _to_float(_lt_field(lt, "flight_actualInstrument")),
        "SimulatedInstrument": _to_float(_lt_field(lt, "flight_simulatedInstrument")),
        "DualGiven": _to_float(_lt_field(lt, "flight_dualGiven")),
        "DualReceived": _to_float(_lt_field(lt, "flight_dualReceived")),
        "Simulator": _to_float(_lt_field(lt, "flight_simulator")),
        "135XC": _to_float(lt.get(custom_map.get("135XC", ""), "")) if custom_map.get("135XC") else 0.0,
        "ATPXC": _to_float(lt.get(custom_map.get("ATPXC", ""), "")) if custom_map.get("ATPXC") else 0.0,
        "NightXC": night_xc_val,
    }


def _diff_hours(a: Dict[str, float], b: Dict[str, float], tol: float) -> Dict[str, Tuple[float, float, float]]:
    diffs: Dict[str, Tuple[float, float, float]] = {}
    for k in a.keys():
        av = a.get(k, 0.0)
        bv = b.get(k, 0.0)
        d = av - bv
        if abs(d) > tol:
            diffs[k] = (av, bv, d)
    return diffs


def _fmt_hours(s: str) -> str:
    v = _to_float(s)
    if not v:
        return "0.0"
    return f"{v:.1f}"


def _shorthand(ff: Dict[str, str]) -> str:
    return f"{ff.get('Date','').strip()} {_norm(ff.get('AircraftID',''))} {_fmt_hours(ff.get('TotalTime',''))}"


def _diff_cell(ff: float, lt: float, tol: float) -> str:
    d = ff - lt
    return "--" if abs(d) <= tol else f"{d:.1f}"


def _print_totals_report(*, ff_flights: List[Dict[str, str]], lt_flights: List[Dict[str, str]], ff_totals: Dict[str, float], lt_totals: Dict[str, float], tol: float) -> None:
    """
    Clean monospace report (intentionally minimal / human scannable).
    """
    rows: List[Tuple[str, float, float]] = [
        ("Total Time", ff_totals["TotalTime"], lt_totals["TotalTime"]),
        ("PIC", ff_totals["PIC"], lt_totals["PIC"]),
        ("Night", ff_totals["Night"], lt_totals["Night"]),
        ("Solo", ff_totals["Solo"], lt_totals["Solo"]),
        ("XC", ff_totals["CrossCountry"], lt_totals["CrossCountry"]),
        ("Actual IMC", ff_totals["ActualInstrument"], lt_totals["ActualInstrument"]),
        ("Sim IMC", ff_totals["SimulatedInstrument"], lt_totals["SimulatedInstrument"]),
        ("Dual Given", ff_totals["DualGiven"], lt_totals["DualGiven"]),
        ("Dual Received", ff_totals["DualReceived"], lt_totals["DualReceived"]),
        ("135XC", ff_totals["135XC"], lt_totals["135XC"]),
        ("ATPXC", ff_totals["ATPXC"], lt_totals["ATPXC"]),
        ("NightXC", ff_totals["NightXC"], lt_totals["NightXC"]),
    ]

    print("========= Logbook Comparison Report =========")
    print(f"{'':14}{'ForeFlight':>11}{'LogTen':>10}{'DIFF':>8}")
    for label, ff, lt in rows:
        diff = _diff_cell(ff, lt, tol=tol)
        print(f"{label:<14}{ff:>11.1f}{lt:>10.1f}{diff:>8}")


def _suggest_nearby_matches(
    *,
    missing_side: str,
    date: str,
    aircraft: str,
    total_time: float,
    from_: str,
    to: str,
    candidates: List[Dict[str, str]],
    date_field: str,
    aircraft_field: str,
    total_time_field: str,
    from_field: str,
    to_field: str,
    tol: float,
    max_days: int = 7,
    max_suggestions: int = 2,
) -> List[str]:
    """
    Heuristic helper for when a flight is "missing" on one side due to a date mismatch.
    We DO NOT auto-match; we only print a hint.
    """
    try:
        y, m, d = (int(x) for x in date.split("-"))
    except Exception:
        return []

    def day_key(s: str) -> int:
        try:
            yy, mm, dd = (int(x) for x in s.split("-"))
        except Exception:
            return 10**9
        # Crude but sufficient for "nearby" within same logbook span: compare as ordinal-ish number.
        return yy * 372 + mm * 31 + dd

    base = day_key(date)
    if base >= 10**9:
        return []

    sug: List[Tuple[float, str]] = []
    for r in candidates:
        ac = _norm(r.get(aircraft_field, ""))
        if ac != aircraft:
            continue
        cand_date = r.get(date_field, "").strip()
        dk = day_key(cand_date)
        if dk >= 10**9:
            continue
        day_diff = abs(dk - base)
        if day_diff > max_days:
            continue

        tt = _to_float(r.get(total_time_field, ""))
        if abs(tt - total_time) > tol:
            continue

        r_from = _norm(r.get(from_field, ""))
        r_to = _norm(r.get(to_field, ""))
        route_penalty = 0.0
        if from_ and r_from and from_ != r_from:
            route_penalty += 0.5
        if to and r_to and to != r_to:
            route_penalty += 0.5

        score = day_diff + route_penalty
        label = f"possible {missing_side} match: {cand_date} {ac} {tt:.1f}"
        if r_from or r_to:
            label += f" {r_from}->{r_to}"
        sug.append((score, label))

    sug.sort(key=lambda x: x[0])
    return [s for _, s in sug[:max_suggestions]]



def main(argv: List[str]) -> int:
    if len(argv) < 3:
        print(
            "Usage: python audit_logbooks.py <foreflight_csv> <logten_tsv> "
            "[--tol 0.05] [--custom-night-xc] [--include-sim] [--verbose]"
        )
        return 2

    ff_path = Path(argv[1])
    lt_path = Path(argv[2])

    tol = 0.05
    derived_night_xc = True
    include_sim = False
    verbose = False
    i = 3
    while i < len(argv):
        if argv[i] == "--tol" and i + 1 < len(argv):
            tol = float(argv[i + 1])
            i += 2
        elif argv[i] == "--custom-night-xc":
            derived_night_xc = False
            i += 1
        elif argv[i] == "--include-sim":
            include_sim = True
            i += 1
        elif argv[i] == "--verbose":
            verbose = True
            i += 1
        else:
            print(f"Unknown arg: {argv[i]}")
            return 2

    ff_flights = _read_foreflight_flights(ff_path)
    lt_flights = _read_logten_flights(lt_path)

    if not include_sim:
        ff_before = len(ff_flights)
        lt_before = len(lt_flights)
        ff_flights = [f for f in ff_flights if not _is_sim_aircraft_id(f.get("AircraftID", ""))]
        lt_flights = [r for r in lt_flights if not _is_sim_aircraft_id(_lt_field(r, "aircraft_aircraftID"))]
        ff_removed = ff_before - len(ff_flights)
        lt_removed = lt_before - len(lt_flights)
        if verbose and (ff_removed or lt_removed):
            print(f"Filtered sim entries: ForeFlight -{ff_removed}, LogTen -{lt_removed}")

    if not ff_flights:
        print("No ForeFlight flight rows found.")
        return 1
    if not lt_flights:
        print("No LogTen flight rows found.")
        return 1

    ff_totals = {
        "TotalTime": _sum_field(ff_flights, "TotalTime"),
        "PIC": _sum_field(ff_flights, "PIC"),
        "Night": _sum_field(ff_flights, "Night"),
        "Solo": _sum_field(ff_flights, "Solo"),
        "CrossCountry": _sum_field(ff_flights, "CrossCountry"),
        "ActualInstrument": _sum_field(ff_flights, "ActualInstrument"),
        "SimulatedInstrument": _sum_field(ff_flights, "SimulatedInstrument"),
        "DualGiven": _sum_field(ff_flights, "DualGiven"),
        "DualReceived": _sum_field(ff_flights, "DualReceived"),
        "135XC": _sum_field(ff_flights, "[Hours]135 XC"),
        "ATPXC": _sum_field(ff_flights, "[Hours]ATPXC"),
        "NightXC": _sum_field(ff_flights, "[Hours]Night XC"),
    }

    # IMPORTANT: Custom-time auto-detection should only consider the custom hour buckets,
    # not the full report totals (TotalTime/PIC/etc). If we include those, we "use up"
    # the 20 customTime columns matching unrelated totals and corrupt the mapping.
    ff_custom_targets = {
        "135XC": ff_totals["135XC"],
        "ATPXC": ff_totals["ATPXC"],
        "NightXC": ff_totals["NightXC"],
    }
    custom_map = _pick_best_custom_time_columns(lt_flights, ff_custom_targets)

    if verbose:
        print("=== Detected LogTen custom time mapping (best guess) ===")
        for k in ["135XC", "ATPXC", "NightXC"]:
            col = custom_map.get(k)
            print(f"{k}: {col or '(none)'}")
        if derived_night_xc:
            print("NightXC: using derived NightXC = flight_night if flight_crossCountry > 0 else 0")

    lt_totals = {
        "TotalTime": sum(_to_float(_lt_field(r, "flight_totalTime")) for r in lt_flights),
        "PIC": sum(_to_float(_lt_field(r, "flight_pic")) for r in lt_flights),
        "Night": sum(_to_float(_lt_field(r, "flight_night")) for r in lt_flights),
        "Solo": sum(_to_float(_lt_field(r, "flight_solo")) for r in lt_flights),
        "CrossCountry": sum(_to_float(_lt_field(r, "flight_crossCountry")) for r in lt_flights),
        "ActualInstrument": sum(_to_float(_lt_field(r, "flight_actualInstrument")) for r in lt_flights),
        "SimulatedInstrument": sum(_to_float(_lt_field(r, "flight_simulatedInstrument")) for r in lt_flights),
        "DualGiven": sum(_to_float(_lt_field(r, "flight_dualGiven")) for r in lt_flights),
        "DualReceived": sum(_to_float(_lt_field(r, "flight_dualReceived")) for r in lt_flights),
        "135XC": _sum_field(lt_flights, custom_map.get("135XC", "")) if custom_map.get("135XC") else 0.0,
        "ATPXC": _sum_field(lt_flights, custom_map.get("ATPXC", "")) if custom_map.get("ATPXC") else 0.0,
        "NightXC": (
            sum(
                (_to_float(_lt_field(r, "flight_night")) if _to_float(_lt_field(r, "flight_crossCountry")) > 0.0 else 0.0)
                for r in lt_flights
            )
            if derived_night_xc
            else (_sum_field(lt_flights, custom_map.get("NightXC", "")) if custom_map.get("NightXC") else 0.0)
        ),
    }

    _print_totals_report(ff_flights=ff_flights, lt_flights=lt_flights, ff_totals=ff_totals, lt_totals=lt_totals, tol=tol)

    ff_grouped = _group_by_date_aircraft(ff_flights, "Date", "AircraftID")
    lt_grouped = _group_by_date_aircraft(lt_flights, "flight_flightDate", "aircraft_aircraftID")

    all_keys = set(ff_grouped.keys()) | set(lt_grouped.keys())
    flagged: List[Dict[str, str]] = []

    for key in sorted(all_keys, key=lambda k: k.date, reverse=True):
        ff_list = ff_grouped.get(key, [])
        lt_list = lt_grouped.get(key, [])

        if not ff_list:
            for lt in lt_list:
                date = key.date
                ac = key.aircraft
                tt = _fmt_hours(_lt_field(lt, "flight_totalTime"))
                reason = "Missing in ForeFlight"
                flagged.append({"date": date, "aircraft": ac, "tt": tt, "reason": reason})
            continue

        pairs = _match_flights_greedy(ff_list, lt_list)
        used_lt = {id(lt) for _, lt in pairs if lt is not None}
        for lt in lt_list:
            if id(lt) not in used_lt:
                date = key.date
                ac = key.aircraft
                tt = _fmt_hours(_lt_field(lt, "flight_totalTime"))
                flagged.append({"date": date, "aircraft": ac, "tt": tt, "reason": "Unmatched extra in LogTen"})

        for ff, lt in pairs:
            if lt is None:
                flagged.append(
                    {
                        "date": ff.get("Date", "").strip(),
                        "aircraft": _norm(ff.get("AircraftID", "")),
                        "tt": _fmt_hours(ff.get("TotalTime", "")),
                        "reason": "Missing in LogTen",
                    }
                )
                continue

            ff_hours = _extract_hours_foreflight(ff)
            lt_hours = _extract_hours_logten(lt, custom_map=custom_map, derived_night_xc=derived_night_xc)
            diffs = _diff_hours(ff_hours, lt_hours, tol=tol)

            # Ignore landing count-only diffs by simply not computing them.
            if diffs:
                parts = []
                for k in ["TotalTime", "PIC", "Night", "CrossCountry", "ActualInstrument", "SimulatedInstrument", "DualReceived", "DualGiven", "Solo", "135XC", "ATPXC", "NightXC"]:
                    if k in diffs:
                        av, bv, _ = diffs[k]
                        parts.append(f"{k} (FF {av:.1f}, LT {bv:.1f})")
                if not parts:
                    for k, (av, bv, _) in diffs.items():
                        parts.append(f"{k} (FF {av:.1f}, LT {bv:.1f})")

                flagged.append(
                    {
                        "date": ff.get("Date", "").strip(),
                        "aircraft": _norm(ff.get("AircraftID", "")),
                        "tt": _fmt_hours(ff.get("TotalTime", "")),
                        "reason": "; ".join(parts),
                    }
                )

    print("\n=== Flagged Entries ===")
    if not flagged:
        print("(none)")
    else:
        ac_w = max((len(r["aircraft"]) for r in flagged), default=0)
        for r in flagged:
            print(f"- {r['date']} {r['aircraft']:<{ac_w}} {r['tt']:>4} — {r['reason']}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))


