import sys
import json
import csv
import math
from datetime import datetime, timedelta, timezone

import numpy as np
from sgp4.api import Satrec, jday

# ── Config ──────────────────────────────────────────────────────────────────

TLE_FILEPATH = '3le_02-25-26.txt'
JSON_OUT = "passes.json"
PASS_SUMMARY = "pass_summary.csv"

# Target: Prince Sultan Air Base
TARGET_LAT_DEG        =  24.06242524 # sourced to match MizarVision release using Google Earth
TARGET_LON_DEG        =  47.56129747
TARGET_ALT_KM         =  0.481

WINDOW_START          = datetime(2026, 2, 23, 0, 0, 0, tzinfo=timezone.utc)
WINDOW_END            = datetime(2026, 2, 27, 23, 59, 59, tzinfo=timezone.utc)
TIMESTEP_SEC          = 10  

# --- FEASIBILITY CRITERIA ---
MIN_ELEVATION_DEG     = 45      # Elevation angle determine LoS optical path ==> ideal: 90
MAX_OFF_NADIR_DEG     = 15.0    # Nadir (also Elevation Ang.) determine skewness of image ==> ideal: 0
MIN_SUN_ELEVATION_DEG =  40.0   # Sun Elevation Angle determines daylight and quality ==> ideal: 90
MAX_RANGE_KM          = 800.0   # The greater the range, the worse the picture ==> ideal = orbital altitude (minimum)

# --- TARGET CONSTELLATIONS ---
TARGET_CONSTELLATIONS = [
    "pleiades",   # Airbus Group
    "spot",       # Airbus Group
    "jilin",      # Chinese Commercial Constellation (operated by Chang Guang Satellite Technology Corporation)
    "superview",  # Chinese Commercial Constellation
    "gaofen",     # Chinese State "Civilian" System
    "yaogan"      # Chinese Military Reconnaissance
]
EARTH_RADIUS_KM       = 6378.137

# ── Math and Propagation Helpers ───────────────────
def deg2rad(d): return d * math.pi / 180.0
def rad2deg(r): return r * 180.0 / math.pi

def gmst_rad(dt_utc):
    j2000  = datetime(2000, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    t_days = (dt_utc - j2000).total_seconds() / 86400.0
    t_cent = t_days / 36525.0
    
    # GMST at 0h UT1 in degrees (precession terms only, no diurnal)
    gmst0_deg = (100.4606184
                 + 36000.77004 * t_cent
                 + 0.000387933 * t_cent**2
                 - (t_cent**3) / 38710000.0)
    
    # Add Earth's rotation for the UT1 fraction of the day
    # 360.98564724° per solar day (sidereal rate)
    ut1_hours = (dt_utc.hour + dt_utc.minute/60.0 
                 + (dt_utc.second + dt_utc.microsecond/1e6) / 3600.0)
    gmst_deg  = gmst0_deg + 360.98564724 * (ut1_hours / 24.0)
    
    return math.fmod(deg2rad(gmst_deg % 360.0), 2.0 * math.pi)

def eci_to_ecef(r_eci, dt_utc):
    theta = gmst_rad(dt_utc)
    c, s  = math.cos(theta), math.sin(theta)
    R = np.array([[ c,  s, 0.0], [-s,  c, 0.0], [0.0, 0.0, 1.0]])
    return R @ np.asarray(r_eci, dtype=float)

def geodetic_to_ecef(lat_deg, lon_deg, alt_km):
    f  = 1.0 / 298.257223563
    e2 = 2.0*f - f*f
    lat, lon = deg2rad(lat_deg), deg2rad(lon_deg)
    N   = EARTH_RADIUS_KM / math.sqrt(1.0 - e2 * math.sin(lat)**2)
    x   = (N + alt_km) * math.cos(lat) * math.cos(lon)
    y   = (N + alt_km) * math.cos(lat) * math.sin(lon)
    z   = (N * (1.0 - e2) + alt_km) * math.sin(lat)
    return np.array([x, y, z])

def local_up(lat_deg, lon_deg):
    lat, lon = deg2rad(lat_deg), deg2rad(lon_deg)
    return np.array([math.cos(lat) * math.cos(lon), math.cos(lat) * math.sin(lon), math.sin(lat)])

def elevation_and_off_nadir(sat_ecef, target_ecef, t_lat_deg, t_lon_deg):
    to_sat   = sat_ecef - target_ecef
    range_km = np.linalg.norm(to_sat)
    up       = local_up(t_lat_deg, t_lon_deg)
    sin_elev = np.dot(to_sat / range_km, up)
    elev     = rad2deg(math.asin(np.clip(sin_elev, -1.0, 1.0)))
    nadir    = -sat_ecef / np.linalg.norm(sat_ecef)
    cos_on   = np.dot((target_ecef - sat_ecef) / np.linalg.norm(target_ecef - sat_ecef), nadir)
    off_nad  = rad2deg(math.acos(np.clip(cos_on, -1.0, 1.0)))
    return elev, off_nad, range_km

def get_sun_position(dt_utc):
    j2000 = datetime(2000, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    n     = (dt_utc - j2000).total_seconds() / 86400.0
    L_deg = (280.460 + 0.9856474 * n) % 360.0
    g     = deg2rad((357.528 + 0.9856003 * n) % 360.0)
    lam   = deg2rad(L_deg + 1.915 * math.sin(g) + 0.020 * math.sin(2.0 * g))
    eps   = deg2rad(23.439 - 0.0000004 * n)
    sun_eci  = np.array([math.cos(lam), math.cos(eps) * math.sin(lam), math.sin(eps) * math.sin(lam)])
    return eci_to_ecef(sun_eci, dt_utc) * 149597870.0 

def solar_elevation_at(sun_ecef, target_lat, target_lon):
    up = local_up(target_lat, target_lon)
    sun_dir = sun_ecef / np.linalg.norm(sun_ecef)
    return rad2deg(math.asin(np.clip(np.dot(sun_dir, up), -1.0, 1.0)))

def parse_3le(filepath, target_constellations):
    candidates = {}
    with open(filepath) as f: lines = [ln.rstrip() for ln in f if ln.strip()]
    i = 0
    while i < len(lines) - 2:
        name, l1, l2 = lines[i], lines[i+1], lines[i+2]
        name = name[2:] # the first characters of the name line are "0 "
        if l1.startswith("1 ") and l2.startswith("2 "):
            # Check if ANY of our target names are in the satellite name
            name_lower = name.lower()
            if any(target.lower() in name_lower for target in target_constellations) and \
                                "deb" not in name_lower:
                
                candidates.setdefault(name.strip(), []).append((l1, l2))
            i += 3
        else: i += 1
    return [(name, Satrec.twoline2rv(entries[0][0], entries[0][1])) for name, entries in candidates.items()]

def precompute_vector(dt):
    '''
    Does the time-dependent math and some matrix prep (equivalent to eci_to_ecef()) for the time vector, 
    so it doesn't have to be repeated for every satellite. 
    '''
    jd, fr = jday(
            dt.year, dt.month, dt.day,
            dt.hour, dt.minute, dt.second
        )

    theta = gmst_rad(dt)

    c = math.cos(theta)
    s = math.sin(theta)

    rot = np.array([
        [ c,  s, 0.],
        [-s,  c, 0.],
        [0., 0., 1.]
    ])

    sun_ecef = get_sun_position(dt)

    sun_elev = solar_elevation_at(
        sun_ecef,
        TARGET_LAT_DEG,
        TARGET_LON_DEG
    )

    return (
            dt,
            jd,
            fr,
            rot,
            sun_elev
        )

def run():
    json_output_data = {
        "target": {"lat": TARGET_LAT_DEG, "lon": TARGET_LON_DEG, "alt": TARGET_ALT_KM},
        "window_start": WINDOW_START.isoformat(),
        "window_end": WINDOW_END.isoformat(),
        "satellites": []
    }
    
    target_ecef = geodetic_to_ecef(TARGET_LAT_DEG, TARGET_LON_DEG, TARGET_ALT_KM)

    times = [WINDOW_START + timedelta(seconds=TIMESTEP_SEC * i) 
             for i in range(int((WINDOW_END - WINDOW_START).total_seconds() / TIMESTEP_SEC))]
    
    print("Precomputing time-dependent data...")
    time_data = []
    for dt in times:
        vector = precompute_vector(dt)
        time_data.append(vector)

    print(f"Parsing TLEs at: {TLE_FILEPATH}")
    csv_pass_summaries = []
    satellites = parse_3le(TLE_FILEPATH, TARGET_CONSTELLATIONS)
    sat_index = 0
    num_sats = len(satellites)
    
    print(f"Read {num_sats} satellites. Analyzing...")
    for name, sat in satellites:
        sat_index += 1
        print("\033[K" + f"Analyzing satellite {sat_index}/{num_sats}: {name}", end="\r", flush=True)

        current_pass_points = [] 
        pass_count = 0

        for dt, jd, fr, rot, sun_elev in time_data:
            err, r_eci, _ = sat.sgp4(jd, fr)
            if err != 0: continue

            sat_ecef = rot @ r_eci
            elev, offnad, rng = elevation_and_off_nadir(sat_ecef, target_ecef, TARGET_LAT_DEG, TARGET_LON_DEG)

            feasible = (elev >= MIN_ELEVATION_DEG and 
                        offnad <= MAX_OFF_NADIR_DEG and 
                        sun_elev >= MIN_SUN_ELEVATION_DEG and 
                        rng <= MAX_RANGE_KM)

            if feasible:
                current_pass_points.append({
                    "time": dt, 
                    "ecef": [sat_ecef[0]*1000, sat_ecef[1]*1000, sat_ecef[2]*1000],
                    "elev": elev, "sun_elev": sun_elev, "offnad": offnad, "rng": rng
                })
            else:
                # If pass just ended, save it as a unique entity for the viewer
                if len(current_pass_points) > 0:
                    suffix = f" (Pass {pass_count + 1})" if pass_count > 0 else ""
                    
                    # Add to JSON list as a standalone "satellite"
                    json_output_data["satellites"].append({
                        "name": f"{name}{suffix}",
                        "trajectory": [{"time": p["time"].isoformat(), "ecef": p["ecef"], "feasible": 1} for p in current_pass_points]
                    })
                    
                    # Add to CSV summary
                    csv_pass_summaries.append({
                        "Satellite": name,
                        "Pass_Start_UTC": current_pass_points[0]["time"].strftime("%Y-%m-%d %H:%M:%S"),
                        "Pass_End_UTC": current_pass_points[-1]["time"].strftime("%Y-%m-%d %H:%M:%S"),
                        "Duration_Seconds": (current_pass_points[-1]["time"] - current_pass_points[0]["time"]).total_seconds() + TIMESTEP_SEC,
                        "Min_Sun_Elevation_deg": round(min([p["sun_elev"] for p in current_pass_points]), 2),
                        "Max_Elevation_deg": round(max([p["elev"] for p in current_pass_points]), 2),
                        "Min_Off_Nadir_deg": round(min([p["offnad"] for p in current_pass_points]), 2),
                        "Min_Slant_Range_km": round(min([p["rng"] for p in current_pass_points]), 2)
                    })
                    
                    current_pass_points = [] 
                    pass_count += 1

        # Handle passes that may be active at the final time step
        if len(current_pass_points) > 0:
            suffix = f" (Pass {pass_count + 1})" if pass_count > 0 else ""
            json_output_data["satellites"].append({
                "name": f"{name}{suffix}",
                "trajectory": [{"time": p["time"].isoformat(), "ecef": p["ecef"], "feasible": 1} for p in current_pass_points]
            })

    # Export Files
    with open(JSON_OUT, "w") as f:
        json.dump(json_output_data, f)
        
    if csv_pass_summaries:
        csv_pass_summaries = sorted(csv_pass_summaries, key=lambda x: x["Pass_Start_UTC"])
        with open(PASS_SUMMARY, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=csv_pass_summaries[0].keys())
            writer.writeheader()
            writer.writerows(csv_pass_summaries)
            
if __name__ == "__main__":
    run()