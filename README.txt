# SPARE: Systematic Portfolio Attribution & Risk Engine

A modular, production-grade quantitative research workflow engineered to ingest high-frequency asset classes, perform vectorized ex-ante portfolio risk metrics, diagnose performance using institutional attribution frameworks, and simulate dynamic tactical factor rotations.

## 🚀 System Architecture & Core Modules
* **Data Ingestion Pipeline (`src/data_fetch.py`)**: Robust automated ETL wrapper handling corporate rebrands, demergers, and listing lifecycles via high-performance compressed Parquet layers.
* **Vectorized Returns Engine (`src/returns_engine.py`)**: Aligns multi-asset historical timeseries, applying padding masks and calculating time-additive log returns for downstream statistical modeling.
* **Portfolio Risk Metrics (`src/risk_engine.py`)**: Implements microsecond-scale matrix multiplication ($w^T \Sigma w$) to compute annualized covariance and portfolio volatility. Operates concurrent 99% Parametric and Historical VaR matrices to calculate Tail Risk Expansion ratios.
* **Brinson-Fachler Attribution (`src/attribution_engine.py`)**: Deconstructs multi-period active alpha returns into isolated Sector Allocation, Security Selection, and Interaction effects.
* **Tactical Factor Rotator (`src/dynamic_portfolio.py`)**: A programmatic rebalancing engine executing a multi-factor Risk-Adjusted Momentum strategy ($\frac{\text{Return}}{\sigma}$) across a dynamic investable universe.

## 📊 Performance Visualization
*Built with an interactive Streamlit & Plotly UI layer to expose backend mathematical diagnostics.*
[Insert a clean screenshot or GIF of your Streamlit Dashboard here]