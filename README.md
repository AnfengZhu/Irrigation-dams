# Have irrigated areas actually expanded around new irrigation dams?

This repository contains the code used in the analysis for the paper:

**"Have irrigated areas actually expanded around new irrigation dams?"**

The project evaluates whether irrigation dams lead to measurable expansion in irrigated cropland by combining satellite-based indicators, spatial analysis, and statistical modeling.

---

## Workflow Overview

The analysis consists of three main steps:

### 1. Google Earth Engine Processing

- Script: `gee_irrigation_dam_analysis.py`
- Extracts spatial indicators (e.g., NDVI-based irrigation signals)
- Processes satellite imagery and command areas

---

### 2. Zonal Statistics & Spatial Aggregation

- Notebook: `Zonal_statistics_command_area.ipynb`
- Computes zonal statistics for command areas
- Aggregates irrigation and cropland statistics across spatial units

---

### 3. Visualization and Statistical Analysis

- Notebook: `Irrigated_cropland_areas_visualization.ipynb`

---

## Contributors

- Endalkachew Kebede
- Marc Müller
- Piyush Mehta
- Bhoktear Khan
- John Uponi
- Anfeng Zhu
- Kyle Davis

---

## Requirements

Typical dependencies include:

### Python

- numpy
- pandas
- geopandas
- rasterio
- matplotlib

### Google Earth Engine

- JavaScript API (Code Editor)

---

## Contact Information

For questions, feedback, or to report issues with the dataset, please contact:

**Kyle Frankel Davis** — corresponding author  
Email: `kfdavis@udel.edu`

**Endalkachew Abebe Kebede**  
Email: `endiabe@udel.edu`
