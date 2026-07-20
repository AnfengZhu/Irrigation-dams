#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Potential command area simulation and SHP exports.
"""

from __future__ import annotations
import argparse
import csv
import sys
from datetime import datetime
from pathlib import Path
import ee


def read_yield_rows(path_text):
    path = Path(path_text)
    if not path.exists():
        raise FileNotFoundError(path)
    rows = []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            country = str(row["country"]).strip()
            cap_bin = str(row["capacity_bin"]).strip()
            y = str(row["estimated_yield_m2_per_m3"]).strip()
            if country and cap_bin and y:
                rows.append({"country": country, "capacity_bin": cap_bin, "Yield": float(y)})
    if not rows:
        raise ValueError(f"No usable yield rows: {path}")
    return rows


def parse_feature_ids(text):
    out = []
    for item in [x.strip() for x in str(text).split(",") if x.strip()]:
        try:
            out.append(int(float(item)))
        except ValueError:
            out.append(item)
    return out


def sanitize_value(value):
    text = ee.Algorithms.If(ee.Algorithms.IsEqual(value, None), "", ee.String(value))
    missing = ee.List(["", "-99", "-9999", "<NA>", "nan", "NaN", "None", "null"])
    return ee.Algorithms.If(missing.contains(text), 0, ee.Number.parse(text))


def positive_number(value):
    return ee.Number(sanitize_value(value)).max(0)


def add_valid_lake_flag(feature):
    # normalize validLake to numeric 0/1 before filtering.
    # Some assets store validLake as Boolean true/false; others store 1/0.
    feature = ee.Feature(feature)
    raw = feature.get("validLake")
    valid_num = ee.Number(
        ee.Algorithms.If(
            ee.Algorithms.IsEqual(raw, None),
            0,
            ee.Algorithms.If(
                ee.Algorithms.IsEqual(raw, False),
                0,
                ee.Algorithms.If(ee.Algorithms.IsEqual(raw, 0), 0, 1),
            ),
        )
    )
    return feature.set("_validLakeNum", valid_num)


def first_text(feature, fields, default=""):
    # text for the first non-empty field in fields list, or default if none found
    value = ee.String(default)
    for field in reversed(fields):
        raw = feature.get(field)
        candidate = ee.String(ee.Algorithms.If(ee.Algorithms.IsEqual(raw, None), "", raw))
        value = ee.String(ee.Algorithms.If(candidate.length().gt(0), candidate, value))
    return value


def first_existing_number(stats, keys):
    # return the first existing number from stats dictionary for the given keys list, or 0 if none found
    stats = ee.Dictionary(stats)
    keys = ee.List(keys)

    def step(key, current):
        key = ee.String(key)
        current = ee.Number(current)
        value = ee.Algorithms.If(stats.contains(key), stats.get(key), None)
        value = ee.Algorithms.If(ee.Algorithms.IsEqual(value, None), 0, value)
        return ee.Number(ee.Algorithms.If(current.neq(0), current, value))

    return ee.Number(keys.iterate(step, 0))


def estimate_reservoir_elevation(reservoir, dem, resol):
    # elveation estimate for the reservoir, using a buffer of resol around the reservoir geometry to avoid edge effects
    reservoir = ee.Feature(reservoir)
    shoreline = reservoir.geometry().buffer(resol).difference(reservoir.geometry().buffer(-resol), resol)
    elev_stats = dem.select("elevation").reduceRegion(
        reducer=ee.Reducer.percentile([50]),
        geometry=shoreline,
        scale=resol,
        maxPixels=1e13,
        tileScale=4,
    )
    return first_existing_number(elev_stats, ["elevation", "elevation_p50"])


def estimate_reservoir_height(reservoir, dem, resol):
    reservoir = ee.Feature(reservoir)
    res_elev = estimate_reservoir_elevation(reservoir, dem, resol)
    elev_stats = dem.select("elevation").reduceRegion(
        reducer=ee.Reducer.percentile([90]),
        geometry=reservoir.geometry().buffer(500),
        scale=resol,
        maxPixels=1e13,
        tileScale=4,
    )
    elev_500m = first_existing_number(elev_stats, ["elevation", "elevation_p90"])
    return elev_500m.subtract(res_elev).max(0)


def add_backup_ratio_calibration(reservoir, dem, resol):
    # Calibrate global backup ratio
    # Only reservoirs with positive observed capacity, positive DEM height, and positive area contribute.
    reservoir = ee.Feature(reservoir)
    cap = positive_number(reservoir.get("ResCapBath"))
    height = estimate_reservoir_height(reservoir, dem, resol)
    area = positive_number(reservoir.get("area"))
    ratio = ee.Number(
        ee.Algorithms.If(
            cap.gt(0).And(height.gt(0)).And(area.gt(0)),
            cap.divide(height.multiply(area)),
            None,
        )
    )
    return reservoir.set("BackupRatioCal", ratio)


def capacity_bin_from_m3(cap_m3):
    cap_mcm = ee.Number(cap_m3).divide(1e6)
    return ee.String(
        ee.Algorithms.If(
            cap_mcm.lt(1),
            "<1",
            ee.Algorithms.If(
                cap_mcm.lt(10),
                "1-10",
                ee.Algorithms.If(
                    cap_mcm.lt(100),
                    "10-100",
                    ee.Algorithms.If(cap_mcm.lt(1000), "100-1000", ">1000"),
                ),
            ),
        )
    )


def get_yield_info(cap_m3, country_name, yields_fc):
    cap_bin = capacity_bin_from_m3(cap_m3)
    country_match = yields_fc.filter(ee.Filter.eq("country", country_name)).filter(
        ee.Filter.eq("capacity_bin", cap_bin)
    )
    global_match = yields_fc.filter(ee.Filter.eq("country", "GLOBAL")).filter(
        ee.Filter.eq("capacity_bin", cap_bin)
    )
    has_country = country_match.size().gt(0)
    has_global = global_match.size().gt(0)
    yield_value = ee.Number(
        ee.Algorithms.If(
            has_country,
            ee.Feature(country_match.first()).get("Yield"),
            ee.Algorithms.If(has_global, ee.Feature(global_match.first()).get("Yield"), -1),
        )
    )
    yield_source = ee.String(
        ee.Algorithms.If(has_country, "COUNTRY", ee.Algorithms.If(has_global, "GLOBAL", "MISSING"))
    )
    return yield_value, yield_source, yield_value.gt(0), cap_bin


def merge_fcs(collections):
    # merge multiple FeatureCollections into one
    out = ee.FeatureCollection([])
    for fc in collections:
        out = out.merge(fc)
    return out


def parse_args():
    parser = argparse.ArgumentParser(description="Submit reservoir-only merged-database CA SHP exports.")
    parser.add_argument("--project", default="Insert your project name here")
    parser.add_argument("--reservoir-asset", default="Insert the reservoir asset path here")
    parser.add_argument("--yield-file", default=(r"Insert the yield CSV file path here"))
    parser.add_argument("--dz", type=int, default=10, choices=[10, 50])
    parser.add_argument("--min-ca-area", type=float, default=200000)
    parser.add_argument("--sample-size", type=int, default=100)
    parser.add_argument("--batch-size", type=int, default=0, help="Reservoirs per production batch. 0 disables batching.")
    parser.add_argument("--batch-index", type=int, default=1, help="1-based production batch index.")
    parser.add_argument("--sample-ids", default="", help="Optional comma-separated merged Feature_ID list.")
    parser.add_argument("--drive-folder", default="")
    parser.add_argument("--date-tag", default="")
    parser.add_argument("--task-prefix", default="", help="Optional exact task/file prefix. Default keeps CA05 naming logic.")
    parser.add_argument("--submit", action="store_true")
    return parser.parse_args()


def main():
    args = parse_args()
    ee.Initialize(project=args.project)

    ## Parameters
    RESOL = 30
    SEARCH_RADIUS = 2_000_000
    DISTANCE_MAX_ERROR = 100
    CANAL_BUFFER = 110

    ## Data
    date_tag = args.date_tag or datetime.now().strftime("%Y%m%d")
    drive_folder = args.drive_folder or f"Mehta_CA{args.dz}_{date_tag}"

    reservoirs = ee.FeatureCollection(args.reservoir_asset)
    yields = ee.FeatureCollection([ee.Feature(None, row) for row in read_yield_rows(args.yield_file)])

    dem = ee.ImageCollection("COPERNICUS/DEM/GLO30").select("DEM").mean().rename("elevation")
    lc = ee.Image("COPERNICUS/Landcover/100m/Proba-V-C3/Global/2019").select("discrete_classification")
    crp = lc.eq(40)
    bas5 = ee.FeatureCollection("WWF/HydroSHEDS/v1/Basins/hybas_5")
    bas6 = ee.FeatureCollection("WWF/HydroSHEDS/v1/Basins/hybas_6")
    bas7 = ee.FeatureCollection("WWF/HydroSHEDS/v1/Basins/hybas_7")
    country = ee.FeatureCollection("USDOS/LSIB_SIMPLE/2017")
    canals = merge_fcs([
            ee.FeatureCollection(f"projects/{args.project}/assets/Canal/GRAIN_v1_Africa"),
            ee.FeatureCollection(f"projects/{args.project}/assets/Canal/GRAIN_v1_Americas"),
            ee.FeatureCollection(f"projects/{args.project}/assets/Canal/GRAIN_v1_Asia"),
            ee.FeatureCollection(f"projects/{args.project}/assets/Canal/GRAIN_v1_Europe"),
            ee.FeatureCollection(f"projects/{args.project}/assets/Canal/GRAIN_v1_Oceania")])

    ## Global backup ratio calibration
    # compute one global backup ratio from all reservoirs with positive ResCapBath, positive DEM height, and positive area.
    reservoirs = reservoirs.map(add_valid_lake_flag)
    reservoirs = (reservoirs.filter(ee.Filter.gt("_validLakeNum", 0)).filter(ee.Filter.gt("area", 0)))
    backup_ratio_fc = reservoirs.map(lambda ft: add_backup_ratio_calibration(ft, dem, RESOL))
    backup_ratio_raw = backup_ratio_fc.aggregate_mean("BackupRatioCal")                                          # Backup ratio 1: Mean
    # backup_ratio_raw = backup_ratio_fc.reduceColumns(ee.Reducer.median(), ["BackupRatioCal"]).get("median")    # Backup ratio 2: Median

    backup_ratio_value = ee.Number(backup_ratio_raw).getInfo()
    if backup_ratio_value is None or backup_ratio_value <= 0:
        raise ValueError("Global backup ratio could not be computed from positive ResCapBath, DEM height, and area.")
    backup_ratio_global = ee.Number(backup_ratio_value)
    print(f"Global backup ratio mean computed inside CA06: {backup_ratio_value}")

    ## Sample for test
    if args.sample_ids:
        reservoirs = reservoirs.filter(ee.Filter.inList("Feature_ID", parse_feature_ids(args.sample_ids)))

    ## Batch
    # Create continuous row_ca_id and use it for batching
    reservoirs = reservoirs.sort("Feature_ID")
    total_available = reservoirs.size().getInfo()
    reservoir_list = reservoirs.toList(total_available)
    reservoirs = ee.FeatureCollection(
        ee.List.sequence(0, total_available - 1).map(
            lambda idx: ee.Feature(reservoir_list.get(idx)).set("row_ca_id", ee.Number(idx).add(1))))
    row_ca_id_min = None
    row_ca_id_max = None
    if args.batch_size > 0:
        if args.batch_index < 1:
            raise ValueError("--batch-index must be 1-based and >= 1.")
        row_ca_id_min = (args.batch_index - 1) * args.batch_size + 1
        row_ca_id_max = args.batch_index * args.batch_size
        reservoirs = reservoirs.filter(ee.Filter.gte("row_ca_id", row_ca_id_min)).filter(ee.Filter.lte("row_ca_id", row_ca_id_max))
    elif args.sample_size > 0:
        reservoirs = reservoirs.limit(args.sample_size)

    def get_ca(res0):
        res0 = ee.Feature(res0)
        country_name = first_text(res0, ["Admin0", "COUNTRY", "Country"], "")
        country_name = ee.String(
            ee.Algorithms.If(
                country_name.length().gt(0),
                country_name,
                first_text(ee.Feature(country.filterBounds(res0.geometry()).first()), ["country_na", "name"], "")))
        
        ## Canal
        canal_near = canals.filterBounds(res0.geometry().buffer(CANAL_BUFFER))
        canal_ids = ee.List(canal_near.aggregate_array("grain_id")).distinct()
        has_canal = canal_ids.length().gt(0)
        canals_sel = ee.FeatureCollection(ee.Algorithms.If(has_canal, canals.filter(ee.Filter.inList("grain_id", canal_ids)), ee.FeatureCollection([])))

        ## ResCapBath capacity
        res_cap_database = positive_number(res0.get("ResCapBath"))
        capacity_database_valid = res_cap_database.gt(0)

        ## Backup capacity
        res_elev_used = estimate_reservoir_elevation(res0, dem, RESOL)
        height = estimate_reservoir_height(res0, dem, RESOL)
        area = positive_number(res0.get("area"))
        backup_cap = ee.Number(
            ee.Algorithms.If(
                backup_ratio_global.gt(0).And(height.gt(0)).And(area.gt(0)),
                height.multiply(area).multiply(backup_ratio_global),
                0))
        backup_capacity_valid = backup_cap.gt(0)

        # Average of ResCapBath and backup capacity
        res_cap = ee.Number(
            ee.Algorithms.If(
                capacity_database_valid.And(backup_capacity_valid),
                res_cap_database.add(backup_cap).divide(2),
                ee.Algorithms.If(
                    backup_capacity_valid,
                    backup_cap,
                    ee.Algorithms.If(capacity_database_valid, res_cap_database, 0))))
        capacity_valid = res_cap.gt(0)

        ## Constrain 1: Basin selection
        bas_level = ee.FeatureCollection(
            ee.Algorithms.If(
                res_cap.lt(100e6),
                bas7,
                ee.Algorithms.If(res_cap.lte(1000e6), bas6, bas5)))
        bas_res = bas_level.filterBounds(res0.geometry())
        bas_can = bas_level.filterBounds(canals_sel.geometry())
        bas_domain = ee.FeatureCollection(ee.Algorithms.If(has_canal, bas_res.merge(bas_can).distinct(["HYBAS_ID"]), bas_res))
        basin_valid = bas_domain.size().gt(0)
        safe_geom = ee.Geometry(ee.Algorithms.If(basin_valid, bas_domain.geometry(), res0.geometry().centroid(RESOL).buffer(RESOL)))
        
        ## Constrain 2: DEM
        dem_limit = ee.Image(1).mask(dem.select("elevation").lt(res_elev_used.add(args.dz)))

        ## Constrain 3: Natyional boundary
        countries = country.filterBounds(res0.geometry())

        ## CA0
        res_mask = ee.FeatureCollection([res0]).reduceToImage(["Feature_ID"], ee.Reducer.count())
        ca0_valid = dem_limit.clipToCollection(bas_domain).clip(countries).subtract(res_mask)
        ca0_valid = ca0_valid.mask(ca0_valid)
        ca0 = ee.Image(ee.Algorithms.If(basin_valid, ca0_valid, ee.Image(0).selfMask()))

        yield_value, yield_source, yield_valid, cap_bin = get_yield_info(res_cap, country_name, yields)
        theory_ca = yield_value.max(0).multiply(res_cap.max(0))
        estimated_ca_raw = (
            ee.Image.pixelArea()
            .updateMask(ca0.multiply(crp))
            .reduceRegion(reducer=ee.Reducer.sum(), geometry=safe_geom, scale=RESOL, maxPixels=1e13, tileScale=4)
            .get("area"))

        estimated_ca = ee.Number(ee.Algorithms.If(ee.Algorithms.IsEqual(estimated_ca_raw, None), 0, estimated_ca_raw)) 
        r_area_pct = ee.Number(ee.Algorithms.If(estimated_ca.gt(0), theory_ca.divide(estimated_ca), 0)).max(0).min(1)

        res_dist = ee.FeatureCollection([res0]).distance(searchRadius=SEARCH_RADIUS, maxError=DISTANCE_MAX_ERROR)
        max_dist_raw = res_dist.mask(ca0.multiply(crp)).reduceRegion(
            reducer=ee.Reducer.percentile([r_area_pct.multiply(100)]),
            geometry=safe_geom,
            scale=RESOL,
            maxPixels=1e13,
            tileScale=4,
        ).get("distance")
        max_dist = ee.Number(
            ee.Algorithms.If(
                ee.Algorithms.IsEqual(max_dist_raw, None),
                0,
                ee.Algorithms.If(estimated_ca.eq(0), 0, max_dist_raw),
            )
        )

        ca = ee.Image(1).mask(res_dist.lte(max_dist).multiply(ca0).multiply(crp))
        vectors = (
            ca.reduceToVectors(
                geometry=res0.geometry().buffer(max_dist.add(RESOL * 2), RESOL),
                scale=RESOL,
                geometryType="polygon",
                eightConnected=True,
                bestEffort=True,
                maxPixels=1e15,
                tileScale=4,
            )
            .map(lambda ft: ft.set("area", ft.geometry().area(RESOL)))
            .filter(ee.Filter.gt("area", args.min_ca_area))
        )
        has_vector = vectors.size().gt(0)
        valid_ca = capacity_valid.And(yield_valid).And(basin_valid).And(estimated_ca.gt(0)).And(max_dist.gt(0)).And(has_vector)
        valid_ca_num = ee.Number(ee.Algorithms.If(valid_ca, 1, 0))
        geom = ee.Geometry(
            ee.Algorithms.If(valid_ca, vectors.geometry(RESOL), res0.geometry().centroid(RESOL).buffer(1))
        )
        ca_area = ee.Number(ee.Algorithms.If(valid_ca, geom.area(RESOL), 0))

        return ee.Feature(
            geom,
            {
                "Feature_ID": res0.get("Feature_ID"),
                "row_ca_id": res0.get("row_ca_id"),
                "Dam_Name": res0.get("Dam_Name"),
                "Admin0": country_name,
                "area": ca_area,
                "validCA": valid_ca_num,
                "empty_ca": ee.Number(ee.Algorithms.If(valid_ca, 0, 1)),
                "MaxDist": max_dist,
                "RAreaPct": r_area_pct,
                "dH": args.dz,
                "Yield": yield_value,
                "YieldSource": yield_source,
                "YieldValid": ee.Number(ee.Algorithms.If(yield_valid, 1, 0)),
                "TheoryCA": theory_ca,
                "EstimatedCA": estimated_ca,
                "HeightUsed": height,
                "ResElevUse": res_elev_used,
                "CapacityVa": ee.Number(ee.Algorithms.If(capacity_valid, 1, 0)),
                "BasinValid": ee.Number(ee.Algorithms.If(basin_valid, 1, 0)),
                "hasCanal": ee.Number(ee.Algorithms.If(has_canal, 1, 0)),
                "capacityBi": cap_bin,
                "ResArea": area,
                "ResCapBath": res_cap_database,
                "BackupCap": backup_cap,
                "ResCapUsed": res_cap,
                "BackupRat": backup_ratio_global,
                "repDataset": res0.get("rep_datase"),
                "sourceComp": res0.get("source_com"),
            },
        )

    total_reservoirs = reservoirs.size().getInfo()
    batch_label = f"B{args.batch_index:03d}" if args.batch_size > 0 else f"test{total_reservoirs}"
    print(f"Reservoirs to export: {total_reservoirs}")
    print(f"Total available before batching: {total_available}")
    if args.batch_size > 0:
        print(f"Batch: {batch_label}; row_ca_id range: {row_ca_id_min}-{row_ca_id_max}; nominal range size: {args.batch_size}; batch index: {args.batch_index}")
    # skip empty row_ca_id ranges instead of submitting an empty SHP export.
    if total_reservoirs == 0:
        print("No valid reservoirs in this row_ca_id range. No GEE task will be submitted.")
        return
    print(f"Reservoir asset: {args.reservoir_asset}")
    print(f"Yield file: {args.yield_file}")
    print(f"DZ: {args.dz}; min patch area: {args.min_ca_area}; Drive folder: {drive_folder}")

    cas = reservoirs.map(get_ca)
    prefix = args.task_prefix or f"MergedCA_CA{args.dz}_{date_tag}_{batch_label}_N{total_reservoirs}"
    task = ee.batch.Export.table.toDrive(
        collection=cas,
        description=prefix[:100],
        folder=drive_folder,
        fileFormat="SHP",
        fileNamePrefix=prefix,
    )
    if args.submit:
        task.start()
        print(f"Submitted SHP task: {task.id}")
    else:
        print("Dry run only. Add --submit to submit the SHP task.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}")
        sys.exit(1)





