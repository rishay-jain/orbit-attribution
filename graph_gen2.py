"""
Static Figure Generator for Paper
------------------------------------
Produces two publication-quality figures from pass_summary.csv + passes.json:

  Figure 1 — Ground track map with satellite passes over Prince Sultan AB,
              colored by time of day (UTC hour of pass start).
              Basemap: ESRI World Imagery satellite tiles.
              Airbus satellites (SPOT, Pleiades) drawn distinctly.

  Figure 2 — Timeline scatter plot: one dot per pass across the 5-day window,
              y-axis = max elevation angle, colored by satellite family,
              sized by pass duration.

Usage:
  python figure_gen.py

Requires: pass_summary.csv, passes.json in working directory.
Dependencies: pip install pandas matplotlib contextily pyproj shapely
"""

import json
import math
import warnings
warnings.filterwarnings("ignore")

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import matplotlib.patheffects as pe
from matplotlib.lines import Line2D
from matplotlib.colors import Normalize
from matplotlib import cm
from datetime import datetime, timezone

try:
    import contextily as ctx
    HAS_CTX = True
except ImportError:
    HAS_CTX = False
    print("contextily not found — figures will use plain background")

# ── Config ───────────────────────────────────────────────────────────────────
TARGET_LAT =  24.06242524
TARGET_LON =  47.56129747
TARGET_NAME = "Prince Sultan AB"

CSV_PATH  = "pass_summary.csv"
JSON_PATH = "passes.json"

# Map extent (degrees) around target
MAP_BUFFER = 3.0   # degrees

# Satellite family classification
FAMILY_MAP = {
    "PLEIADES": ("Airbus (Pleiades/SPOT)", "#FF4C4C"),
    "SPOT":     ("Airbus (Pleiades/SPOT)", "#FF4C4C"),
    "JILIN":    ("Jilin-1 (Chang Guang)", "#00C8FF"),
    "SUPERVIEW":("SuperView",             "#FFD700"),
    "GAOFEN":   ("Gaofen (CNSA)",         "#00FF88"),
    "YAOGAN":   ("Yaogan (PLA)",          "#FF8C00"),
}
DEFAULT_FAMILY = ("Other Chinese",  "#AAAAAA")


def classify(name):
    n = name.upper()
    for key, (label, color) in FAMILY_MAP.items():
        if key in n:
            return label, color
    return DEFAULT_FAMILY


def ecef_to_lla_deg(x, y, z):
    """ECEF (metres) -> (lat_deg, lon_deg). WGS-84."""
    a  = 6378137.0
    f  = 1.0 / 298.257223563
    e2 = 2*f - f*f
    lon = math.atan2(y, x)
    p   = math.hypot(x, y)
    lat = math.atan2(z, p * (1 - e2))
    for _ in range(6):
        N   = a / math.sqrt(1 - e2 * math.sin(lat)**2)
        lat = math.atan2(z + e2 * N * math.sin(lat), p)
    return math.degrees(lat), math.degrees(lon)


def load_data():
    df = pd.read_csv(CSV_PATH)
    df["Pass_Start_UTC"] = pd.to_datetime(df["Pass_Start_UTC"])
    df["Pass_End_UTC"]   = pd.to_datetime(df["Pass_End_UTC"])
    df["UTC_Hour"]       = df["Pass_Start_UTC"].dt.hour + df["Pass_Start_UTC"].dt.minute / 60
    df["Family"], df["Color"] = zip(*df["Satellite"].map(classify))
    df["Day"] = df["Pass_Start_UTC"].dt.day
    df["Duration"] = df["Duration_Seconds"]

    with open(JSON_PATH) as f:
        passes = json.load(f)

    return df, passes


# ── Figure 1: Ground Track Map ───────────────────────────────────────────────
def fig1_ground_tracks(df, passes):
    fig, ax = plt.subplots(figsize=(10, 10), dpi=150)

    def base_name(s):
        return s.split(" (Pass")[0].strip()

    # Chinese-only: exclude Airbus passes from the figures
    chinese_names = set(df[df["Family"] != "Airbus (Pleiades/SPOT)"]["Satellite"])

    # Color by pass duration
    dur_lookup = dict(zip(df["Satellite"], df["Duration_Seconds"]))
    dur_min = df.loc[df["Satellite"].isin(chinese_names), "Duration_Seconds"].min()
    dur_max = df.loc[df["Satellite"].isin(chinese_names), "Duration_Seconds"].max()

    cmap = cm.plasma
    norm = Normalize(vmin=dur_min, vmax=dur_max)

    plotted_any = False
    for sat in passes["satellites"]:
        bname = base_name(sat["name"])
        if bname not in chinese_names:
            continue  # skip Airbus passes

        duration = dur_lookup.get(bname, dur_min)

        lats, lons = [], []
        for pt in sat["trajectory"]:
            lat, lon = ecef_to_lla_deg(*pt["ecef"])
            lats.append(lat)
            lons.append(lon)

        if not lats:
            continue

        # Break track at antimeridian
        segs_lat, segs_lon = [[]], [[]]
        for i in range(len(lons)):
            if i > 0 and abs(lons[i] - lons[i-1]) > 180:
                segs_lat.append([])
                segs_lon.append([])
            segs_lat[-1].append(lats[i])
            segs_lon[-1].append(lons[i])

        color = cmap(norm(duration))

        for slat, slon in zip(segs_lat, segs_lon):
            ax.plot(slon, slat, color=color, linewidth=2.0,
                    linestyle="-", alpha=0.80, zorder=3)
        plotted_any = True

    # Target marker
    ax.scatter(TARGET_LON, TARGET_LAT, color="red", s=200,
               marker="*", zorder=10, linewidths=0.8,
               edgecolors="white", label=TARGET_NAME)
    ax.annotate(TARGET_NAME,
                xy=(TARGET_LON, TARGET_LAT),
                xytext=(TARGET_LON + 0.15, TARGET_LAT + 0.15),
                color="white", fontsize=9, fontweight="bold",
                path_effects=[pe.withStroke(linewidth=2, foreground="black")])

    # Map extent
    ax.set_xlim(TARGET_LON - MAP_BUFFER, TARGET_LON + MAP_BUFFER)
    ax.set_ylim(TARGET_LAT - MAP_BUFFER, TARGET_LAT + MAP_BUFFER)

    # Basemap
    if HAS_CTX:
        try:
            ctx.add_basemap(ax,
                            crs="EPSG:4326",
                            source=ctx.providers.Esri.WorldImagery,
                            zoom=7,
                            attribution=False)
        except Exception as e:
            print(f"Basemap error: {e} — trying OpenStreetMap fallback")
            try:
                ctx.add_basemap(ax, crs="EPSG:4326",
                                source=ctx.providers.OpenStreetMap.Mapnik,
                                attribution=False)
            except Exception:
                ax.set_facecolor("#1a1a2e")

    # Colorbar for pass duration
    sm = plt.cm.ScalarMappable(cmap=cmap, norm=norm)
    sm.set_array([])
    cbar = fig.colorbar(sm, ax=ax, fraction=0.03, pad=0.01)
    cbar.set_label("Pass Duration (seconds)", color="white", fontsize=10)
    cbar.ax.yaxis.set_tick_params(color="white")
    plt.setp(cbar.ax.yaxis.get_ticklabels(), color="white")

    # Legend entries
    legend_elements = [
        Line2D([0],[0], color=cmap(norm(dur_min)),
               linewidth=2, label=f"Short pass (~{int(dur_min)}s)"),
        Line2D([0],[0], color=cmap(norm((dur_min+dur_max)/2)),
               linewidth=2, label=f"Medium pass (~{int((dur_min+dur_max)/2)}s)"),
        Line2D([0],[0], color=cmap(norm(dur_max)),
               linewidth=2, label=f"Long pass (~{int(dur_max)}s)"),
        Line2D([0],[0], marker="*", color="red", markersize=12,
               linestyle="None", label=TARGET_NAME),
    ]

    leg = ax.legend(handles=legend_elements, loc="lower left",
                    facecolor="#111111CC", edgecolor="#444",
                    labelcolor="white", fontsize=8.5,
                    framealpha=0.85)

    ax.set_xlabel("Longitude (°E)", color="white", fontsize=10)
    ax.set_ylabel("Latitude (°N)", color="white", fontsize=10)
    ax.tick_params(colors="white")
    for spine in ax.spines.values():
        spine.set_edgecolor("#555")

    ax.set_title(
        "Chinese Satellite Feasible Collection Passes over Prince Sultan AB  (Feb 23–27, 2026)\n"
        "Criteria: elevation ≥45°  |  off-nadir ≤15°  |  solar elevation ≥40°",
        color="white", fontsize=11, pad=10
    )
    fig.patch.set_facecolor("#0d1117")
    ax.set_facecolor("#0d1117")

    plt.tight_layout()
    out = "figure1_ground_tracks.png"
    plt.savefig(out, dpi=200, bbox_inches="tight", facecolor="#0d1117")
    print(f"Saved: {out}")
    plt.close()


# ── Figure 2: Timeline / Opportunity Plot ────────────────────────────────────
def fig2_timeline(df):
    # Chinese satellites only
    df = df[df["Family"] != "Airbus (Pleiades/SPOT)"].copy()

    fig, ax = plt.subplots(figsize=(14, 6), dpi=150)
    fig.patch.set_facecolor("#0d1117")
    ax.set_facecolor("#0d1117")

    # Unique families for legend
    family_colors = {}
    for _, row in df.iterrows():
        family_colors[row["Family"]] = row["Color"]

    # Normalize duration for dot size
    dur_min = df["Duration_Seconds"].min()
    dur_max = df["Duration_Seconds"].max()
    def dur_to_size(d):
        return 40 + 160 * (d - dur_min) / max(dur_max - dur_min, 1)

    # x-axis: hours since window start
    t0 = df["Pass_Start_UTC"].min().normalize()
    df["x"] = (df["Pass_Start_UTC"] - t0).dt.total_seconds() / 3600

    for _, row in df.iterrows():
        ax.scatter(row["x"], row["Max_Elevation_deg"],
                   color=row["Color"],
                   s=dur_to_size(row["Duration_Seconds"]),
                   zorder=3,
                   edgecolors=row["Color"],
                   linewidths=0.3,
                   alpha=0.95)
        
    # Day boundary lines
    for d in range(1, 6):
        x_day = d * 24
        ax.axvline(x_day, color="#333", linewidth=0.8, linestyle="--", zorder=1)

    # X-axis ticks: one per day
    day_labels = ["Feb 23", "Feb 24", "Feb 25", "Feb 26", "Feb 27"]
    tick_positions = [d * 24 + 6 for d in range(5)]  # ~06:00 each day (start of window)
    ax.set_xticks(tick_positions)
    ax.set_xticklabels(day_labels, color="white", fontsize=10)
    ax.set_xlim(0, 5 * 24)

    ax.set_ylim(70, 92)
    ax.set_ylabel("Max Elevation Angle (°)", color="white", fontsize=11)
    ax.tick_params(colors="white")
    for spine in ax.spines.values():
        spine.set_edgecolor("#333")

    # Legend: families
    legend_elements = [
        mpatches.Patch(facecolor=color, edgecolor="white",
                       linewidth=0.5, label=family)
        for family, color in sorted(family_colors.items())
    ]
    # Size legend
    for dur_val, label in [(20, "20s pass"), (60, "60s pass"), (130, "130s pass")]:
        legend_elements.append(
            Line2D([0],[0], marker="o", color="none",
                   markerfacecolor="#888", markersize=math.sqrt(dur_to_size(dur_val)),
                   label=label)
        )
    leg = ax.legend(handles=legend_elements, loc="lower right",
                    facecolor="#111111CC", edgecolor="#444",
                    labelcolor="white", fontsize=8.5,
                    framealpha=0.85, ncol=2)

    ax.set_title(
        "Satellite Collection Opportunities over Prince Sultan AB  (Feb 23–27, 2026)\n"
        "Each point = one feasible pass  |  Size = pass duration  |  "
        "Airbus satellites outlined in white",
        color="white", fontsize=11, pad=10
    )

    # Horizontal reference lines
    ax.axhline(75, color="#444", linewidth=0.6, linestyle=":")
    ax.axhline(85, color="#444", linewidth=0.6, linestyle=":")

    plt.tight_layout()
    out = "figure2_timeline.png"
    plt.savefig(out, dpi=200, bbox_inches="tight", facecolor="#0d1117")
    print(f"Saved: {out}")
    plt.close()


# ── Main ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("Loading data...")
    df, passes = load_data()
    print(f"  {len(df)} passes, {len(passes['satellites'])} trajectory segments\n")

    print("Generating Figure 1: Ground tracks...")
    fig1_ground_tracks(df, passes)

    print("Generating Figure 2: Timeline...")
    fig2_timeline(df)

    print("\nDone.")