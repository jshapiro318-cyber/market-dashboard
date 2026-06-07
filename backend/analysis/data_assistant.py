"""Data Assistant Analysis Engine — Claude Data Analysis Assistant.

Implements all 6 sub-agent capabilities:
  1. Data Explorer     – summary stats, schema, sample data
  2. Visualization Spec – aggregations for chart rendering
  3. Code Generator    – Python, R, SQL, JavaScript templates
  4. Report Writer     – executive Markdown report
  5. Quality Assurance – quality scoring, outlier detection, data profiling
  6. Hypothesis Generator – research hypothesis cards based on data patterns
"""
from __future__ import annotations

import math
import os
import numpy as np
import pandas as pd
from datetime import datetime


# ─────────────────────────────────────────────────────────────────────────────
# 1. DATA EXPLORER — summary stats + schema + aggregations
# ─────────────────────────────────────────────────────────────────────────────

def get_summary_stats(df: pd.DataFrame) -> dict:
    """Analyze the dataframe and return column details, statistics, and correlation matrix."""
    row_count = len(df)
    col_count = len(df.columns)

    columns_info = []
    numeric_cols = []
    categorical_cols = []

    for col in df.columns:
        null_count = int(df[col].isnull().sum())
        unique_count = int(df[col].nunique())
        dtype = str(df[col].dtype)

        info = {
            "name": col,
            "type": dtype,
            "null_count": null_count,
            "null_pct": round((null_count / row_count * 100), 2) if row_count else 0.0,
            "unique_count": unique_count,
        }

        if np.issubdtype(df[col].dtype, np.number):
            numeric_cols.append(col)
            info["mean"] = float(df[col].mean()) if not df[col].empty and not pd.isna(df[col].mean()) else None
            info["std"] = float(df[col].std()) if not df[col].empty and len(df[col]) > 1 and not pd.isna(df[col].std()) else None
            info["min"] = float(df[col].min()) if not df[col].empty and not pd.isna(df[col].min()) else None
            info["max"] = float(df[col].max()) if not df[col].empty and not pd.isna(df[col].max()) else None
            # Skewness and kurtosis
            try:
                info["skewness"] = round(float(df[col].skew()), 3)
                info["kurtosis"] = round(float(df[col].kurt()), 3)
            except Exception:
                info["skewness"] = None
                info["kurtosis"] = None
        else:
            categorical_cols.append(col)
            if unique_count > 0:
                top_val = df[col].value_counts().index[0]
                top_freq = int(df[col].value_counts().iloc[0])
                info["top_value"] = str(top_val)
                info["top_frequency"] = top_freq
                info["top_pct"] = round((top_freq / row_count * 100), 2) if row_count else 0.0

        columns_info.append(info)

    # Correlation matrix
    correlation = None
    if len(numeric_cols) >= 2:
        valid_cols = [c for c in numeric_cols if df[c].std() > 0]
        if len(valid_cols) >= 2:
            corr_df = df[valid_cols].corr()
            correlation = {
                "columns": valid_cols,
                "grid": [[round(float(corr_df.loc[c1, c2]), 3) for c2 in valid_cols] for c1 in valid_cols]
            }

    sample_data = df.head(10).fillna("").to_dict(orient="records")

    # Aggregations for charts
    aggregations = {}
    for cat in ["location", "device_type", "action"]:
        if cat in df.columns:
            val_counts = df[cat].value_counts().head(8)
            aggregations[cat] = {
                "labels": [str(x) for x in val_counts.index],
                "values": [int(v) for v in val_counts.values]
            }

    if "timestamp" in df.columns:
        try:
            df_temp = df.copy()
            df_temp["date"] = pd.to_datetime(df_temp["timestamp"]).dt.date
            if "revenue" in df_temp.columns:
                df_temp["revenue"] = pd.to_numeric(df_temp["revenue"], errors="coerce").fillna(0.0)
                daily = df_temp.groupby("date").agg({"revenue": "sum", "session_id": "count"}).tail(10)
                aggregations["trend"] = {
                    "labels": [str(d) for d in daily.index],
                    "revenue": [float(r) for r in daily["revenue"].values],
                    "volume": [int(v) for v in daily["session_id"].values]
                }
        except Exception:
            pass

    # Numeric distribution histograms
    for num_col in numeric_cols[:3]:
        try:
            vals = df[num_col].dropna().tolist()
            if len(vals) >= 5:
                mn, mx = min(vals), max(vals)
                step = (mx - mn) / 10 if mx != mn else 1.0
                bins = [0] * 10
                labels = [f"{mn + i*step:.1f}–{mn + (i+1)*step:.1f}" for i in range(10)]
                for v in vals:
                    idx = min(int((v - mn) / step), 9)
                    if idx >= 0:
                        bins[idx] += 1
                aggregations[f"hist_{num_col}"] = {"labels": labels, "values": bins, "column": num_col}
        except Exception:
            pass

    return {
        "row_count": row_count,
        "col_count": col_count,
        "numeric_cols": numeric_cols,
        "categorical_cols": categorical_cols,
        "columns": columns_info,
        "correlation": correlation,
        "sample": sample_data,
        "aggregations": aggregations,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 2. STATISTICAL ANALYSIS — deep numeric insights
# ─────────────────────────────────────────────────────────────────────────────

def run_statistical_analysis(df: pd.DataFrame) -> dict:
    """Compute deep statistical analysis: correlations, variance, skewness, kurtosis."""
    numeric_cols = df.select_dtypes(include=[np.number]).columns.tolist()

    column_stats = []
    for col in numeric_cols:
        series = df[col].dropna()
        if len(series) < 2:
            continue
        stat = {
            "name": col,
            "count": int(len(series)),
            "mean": round(float(series.mean()), 4),
            "median": round(float(series.median()), 4),
            "std": round(float(series.std()), 4),
            "variance": round(float(series.var()), 4),
            "min": round(float(series.min()), 4),
            "max": round(float(series.max()), 4),
            "q25": round(float(series.quantile(0.25)), 4),
            "q75": round(float(series.quantile(0.75)), 4),
            "iqr": round(float(series.quantile(0.75) - series.quantile(0.25)), 4),
            "skewness": round(float(series.skew()), 4),
            "kurtosis": round(float(series.kurt()), 4),
        }
        # Distribution shape
        if abs(stat["skewness"]) < 0.5:
            stat["distribution_shape"] = "Approximately Normal"
        elif stat["skewness"] > 1.5:
            stat["distribution_shape"] = "Highly Right-Skewed"
        elif stat["skewness"] > 0.5:
            stat["distribution_shape"] = "Slightly Right-Skewed"
        elif stat["skewness"] < -1.5:
            stat["distribution_shape"] = "Highly Left-Skewed"
        else:
            stat["distribution_shape"] = "Slightly Left-Skewed"
        column_stats.append(stat)

    # Correlation matrix
    correlation = None
    if len(numeric_cols) >= 2:
        valid = [c for c in numeric_cols if df[c].std() > 0]
        if len(valid) >= 2:
            corr_df = df[valid].corr()
            correlation = {
                "columns": valid,
                "grid": [[round(float(corr_df.loc[c1, c2]), 3) for c2 in valid] for c1 in valid],
            }

    # Strong correlations (|r| > 0.5)
    strong_pairs = []
    if correlation:
        cols = correlation["columns"]
        for i, c1 in enumerate(cols):
            for j, c2 in enumerate(cols):
                if i < j:
                    r = correlation["grid"][i][j]
                    if abs(r) >= 0.5:
                        direction = "positive" if r > 0 else "negative"
                        strength = "strong" if abs(r) >= 0.75 else "moderate"
                        strong_pairs.append({
                            "col1": c1, "col2": c2,
                            "r": r,
                            "direction": direction,
                            "strength": strength,
                        })

    return {
        "type": "statistical",
        "column_stats": column_stats,
        "correlation": correlation,
        "strong_correlations": strong_pairs,
        "numeric_count": len(numeric_cols),
        "categorical_count": len(df.select_dtypes(exclude=[np.number]).columns),
    }


# ─────────────────────────────────────────────────────────────────────────────
# 3. PREDICTIVE ANALYSIS — feature importance, variable suggestions
# ─────────────────────────────────────────────────────────────────────────────

def run_predictive_analysis(df: pd.DataFrame) -> dict:
    """Estimate feature importance via variance ranking + target correlation."""
    numeric_cols = df.select_dtypes(include=[np.number]).columns.tolist()

    # Identify likely target (highest variance or named 'revenue'/'target'/'label')
    target_candidates = [c for c in ["revenue", "target", "label", "churn", "conversion"] if c in numeric_cols]
    target = target_candidates[0] if target_candidates else (numeric_cols[-1] if numeric_cols else None)

    features = []
    for col in numeric_cols:
        if col == target:
            continue
        series = df[col].dropna()
        if len(series) < 5:
            continue

        var_score = float(series.std()) / (abs(float(series.mean())) + 1e-9)
        corr_with_target = 0.0
        if target and target in df.columns:
            try:
                merged = df[[col, target]].dropna()
                if len(merged) >= 5 and merged[col].std() > 0 and merged[target].std() > 0:
                    corr_with_target = round(float(merged[col].corr(merged[target])), 3)
            except Exception:
                pass

        features.append({
            "name": col,
            "variance_score": round(var_score, 3),
            "correlation_with_target": corr_with_target,
            "importance": round((abs(corr_with_target) * 0.7 + min(var_score, 1.0) * 0.3), 3),
        })

    features.sort(key=lambda x: x["importance"], reverse=True)

    # Model recommendations
    recommendations = []
    if target:
        recommendations.append({
            "model": "Random Forest Regressor",
            "reason": f"'{target}' is a continuous numerical target; tree-based ensemble handles non-linearity well.",
            "confidence": "High"
        })
        recommendations.append({
            "model": "Linear Regression",
            "reason": "Baseline interpretable model — useful for understanding coefficient relationships.",
            "confidence": "Medium"
        })
        if len(features) >= 5:
            recommendations.append({
                "model": "Gradient Boosting (XGBoost/LightGBM)",
                "reason": f"With {len(features)} features and likely interactions, boosting typically outperforms single-tree methods.",
                "confidence": "High"
            })

    return {
        "type": "predictive",
        "suggested_target": target,
        "features": features[:10],
        "model_recommendations": recommendations,
        "feature_count": len(features),
    }


# ─────────────────────────────────────────────────────────────────────────────
# 4. QUALITY ASSURANCE — scoring, profiling, outlier detection
# ─────────────────────────────────────────────────────────────────────────────

def run_quality_check(df: pd.DataFrame) -> dict:
    """Comprehensive data quality audit: completeness, uniqueness, outliers, duplicates."""
    row_count = len(df)
    col_count = len(df.columns)

    # Duplicate rows
    dup_count = int(df.duplicated().sum())
    dup_pct = round(dup_count / row_count * 100, 2) if row_count else 0.0

    # Per-column quality
    column_quality = []
    total_cells = row_count * col_count
    null_cells = int(df.isnull().sum().sum())
    completeness_score = round((1 - null_cells / total_cells) * 100, 1) if total_cells else 100.0

    for col in df.columns:
        series = df[col]
        null_count = int(series.isnull().sum())
        null_pct = round(null_count / row_count * 100, 2) if row_count else 0.0
        unique_count = int(series.nunique())
        col_q = {
            "name": col,
            "null_count": null_count,
            "null_pct": null_pct,
            "unique_count": unique_count,
            "completeness": round(100 - null_pct, 1),
            "issues": [],
        }

        # Outlier detection for numeric columns (IQR method)
        outlier_count = 0
        if np.issubdtype(series.dtype, np.number):
            q1 = series.quantile(0.25)
            q3 = series.quantile(0.75)
            iqr = q3 - q1
            lower = q1 - 1.5 * iqr
            upper = q3 + 1.5 * iqr
            outliers = series[(series < lower) | (series > upper)]
            outlier_count = len(outliers)
            col_q["outlier_count"] = outlier_count
            col_q["outlier_pct"] = round(outlier_count / row_count * 100, 2) if row_count else 0.0
            col_q["outlier_bounds"] = {"lower": round(float(lower), 2), "upper": round(float(upper), 2)}
            if outlier_count > 0:
                col_q["issues"].append(f"{outlier_count} outliers detected (IQR method)")

        # Flag high missingness
        if null_pct > 20:
            col_q["issues"].append(f"High missingness: {null_pct}% null")
        elif null_pct > 5:
            col_q["issues"].append(f"Moderate missingness: {null_pct}% null")

        # Flag low cardinality for numeric (likely encoded categorical)
        if np.issubdtype(series.dtype, np.number) and unique_count <= 5 and row_count > 20:
            col_q["issues"].append("Low cardinality — possibly an encoded categorical")

        # Quality grade
        penalty = null_pct * 0.5 + (outlier_count / row_count * 100 * 0.3 if row_count else 0)
        score = max(0, 100 - penalty)
        col_q["quality_score"] = round(score, 1)
        col_q["grade"] = "A" if score >= 90 else "B" if score >= 75 else "C" if score >= 60 else "D"

        column_quality.append(col_q)

    # Overall quality score
    avg_col_score = sum(c["quality_score"] for c in column_quality) / len(column_quality) if column_quality else 100.0
    dup_penalty = dup_pct * 0.5
    overall_score = round(max(0, avg_col_score - dup_penalty), 1)
    overall_grade = "A" if overall_score >= 90 else "B" if overall_score >= 75 else "C" if overall_score >= 60 else "D"

    # Recommendations
    recommendations = []
    if dup_count > 0:
        recommendations.append(f"Remove {dup_count} duplicate rows using df.drop_duplicates()")
    high_null = [c for c in column_quality if c["null_pct"] > 20]
    if high_null:
        cols_str = ", ".join(c["name"] for c in high_null[:3])
        recommendations.append(f"Consider imputation or removal for high-null columns: {cols_str}")
    high_outlier = [c for c in column_quality if c.get("outlier_pct", 0) > 5]
    if high_outlier:
        cols_str = ", ".join(c["name"] for c in high_outlier[:3])
        recommendations.append(f"Review outliers in: {cols_str} — consider capping or log transform")

    return {
        "type": "quality",
        "overall_score": overall_score,
        "overall_grade": overall_grade,
        "completeness_score": completeness_score,
        "duplicate_count": dup_count,
        "duplicate_pct": dup_pct,
        "row_count": row_count,
        "col_count": col_count,
        "column_quality": column_quality,
        "recommendations": recommendations,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 5. HYPOTHESIS GENERATOR — research hypothesis cards
# ─────────────────────────────────────────────────────────────────────────────

def generate_hypotheses(df: pd.DataFrame, domain: str = "general") -> dict:
    """Generate data-driven research hypotheses based on column analysis and domain."""
    domain = domain.lower()
    numeric_cols = df.select_dtypes(include=[np.number]).columns.tolist()
    cat_cols = df.select_dtypes(exclude=[np.number]).columns.tolist()
    has_timestamp = "timestamp" in df.columns or "date" in df.columns

    hypotheses = []

    # Correlation-based hypotheses
    if len(numeric_cols) >= 2:
        try:
            corr = df[numeric_cols].corr()
            for i, c1 in enumerate(numeric_cols):
                for j, c2 in enumerate(numeric_cols):
                    if i < j:
                        r = corr.loc[c1, c2]
                        if abs(r) >= 0.4 and not math.isnan(r):
                            direction = "positively" if r > 0 else "negatively"
                            strength = "strongly" if abs(r) > 0.7 else "moderately"
                            hypotheses.append({
                                "id": f"H{len(hypotheses)+1}",
                                "title": f"{c1} and {c2} are {strength} correlated",
                                "statement": f"Higher values of `{c1}` are associated with {'higher' if r > 0 else 'lower'} values of `{c2}` (r = {r:.2f}).",
                                "type": "Correlation",
                                "confidence": "High" if abs(r) > 0.7 else "Medium",
                                "test_method": "Pearson / Spearman correlation test",
                                "priority": "High" if abs(r) > 0.7 else "Medium",
                            })
                            if len(hypotheses) >= 3:
                                break
                if len(hypotheses) >= 3:
                    break
        except Exception:
            pass

    # Categorical group difference hypotheses
    for cat_col in cat_cols[:2]:
        for num_col in numeric_cols[:2]:
            try:
                groups = df.groupby(cat_col)[num_col].mean()
                if len(groups) >= 2:
                    top = groups.idxmax()
                    bot = groups.idxmin()
                    diff_pct = round((groups.max() - groups.min()) / (abs(groups.min()) + 1e-9) * 100, 1)
                    if diff_pct > 10:
                        hypotheses.append({
                            "id": f"H{len(hypotheses)+1}",
                            "title": f"{cat_col} significantly impacts {num_col}",
                            "statement": f"Users/records in '{top}' group show ~{diff_pct}% higher {num_col} compared to '{bot}' group.",
                            "type": "Group Difference",
                            "confidence": "High" if diff_pct > 50 else "Medium",
                            "test_method": "ANOVA / Kruskal-Wallis test",
                            "priority": "High" if diff_pct > 50 else "Medium",
                        })
            except Exception:
                pass

    # Temporal trend hypothesis
    if has_timestamp and numeric_cols:
        hypotheses.append({
            "id": f"H{len(hypotheses)+1}",
            "title": f"{numeric_cols[0]} shows a temporal trend",
            "statement": f"`{numeric_cols[0]}` values may exhibit time-series patterns (seasonality, trend, or cyclicality) based on the timestamp dimension.",
            "type": "Temporal Trend",
            "confidence": "Medium",
            "test_method": "Time series decomposition (STL) / Mann-Kendall trend test",
            "priority": "Medium",
        })

    # Domain-specific hypotheses
    domain_hyps = {
        "churn-prediction": {
            "title": "Engagement recency predicts churn probability",
            "statement": "Users who have not interacted within the last 30 days are significantly more likely to churn.",
            "type": "Predictive",
            "confidence": "High",
            "test_method": "Logistic Regression / Cox Proportional Hazard Model",
            "priority": "High",
        },
        "user-segmentation": {
            "title": "Behavioral clusters exist within the user base",
            "statement": "Distinct user segments can be identified via behavioral patterns (action frequency, device preference, location) using unsupervised clustering.",
            "type": "Segmentation",
            "confidence": "High",
            "test_method": "K-Means Clustering / DBSCAN / Silhouette Score",
            "priority": "High",
        },
        "revenue-analysis": {
            "title": "Device type is a significant revenue predictor",
            "statement": "Users on specific devices (desktop vs mobile) generate measurably different revenue, suggesting device-optimized UX investment priorities.",
            "type": "Business Impact",
            "confidence": "Medium",
            "test_method": "ANOVA + effect size (Cohen's d)",
            "priority": "High",
        },
        "general": {
            "title": "Data distribution follows a power law in key metrics",
            "statement": "A minority (~20%) of records likely account for the majority (~80%) of the target metric (Pareto principle).",
            "type": "Distribution",
            "confidence": "Medium",
            "test_method": "Pareto analysis / Lorenz curve",
            "priority": "Medium",
        }
    }

    domain_key = next((k for k in domain_hyps if k in domain), "general")
    h = domain_hyps[domain_key].copy()
    h["id"] = f"H{len(hypotheses)+1}"
    hypotheses.append(h)

    # Always add a data quality hypothesis
    null_total = int(df.isnull().sum().sum())
    if null_total > 0:
        hypotheses.append({
            "id": f"H{len(hypotheses)+1}",
            "title": "Missing data is not missing at random (MNAR)",
            "statement": f"The {null_total} null values in the dataset may be systematically related to unobserved variables, biasing downstream models if imputed naïvely.",
            "type": "Data Quality",
            "confidence": "Medium",
            "test_method": "Little's MCAR test / missingness pattern heatmap",
            "priority": "High",
        })

    return {
        "domain": domain,
        "hypothesis_count": len(hypotheses),
        "hypotheses": hypotheses[:8],
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


# ─────────────────────────────────────────────────────────────────────────────
# 6. COMPLETE ANALYSIS — wraps all types
# ─────────────────────────────────────────────────────────────────────────────

def run_complete_analysis(df: pd.DataFrame) -> dict:
    """Run all analysis types and combine into one comprehensive payload."""
    base = get_summary_stats(df)
    statistical = run_statistical_analysis(df)
    predictive = run_predictive_analysis(df)
    quality = run_quality_check(df)
    hypotheses = generate_hypotheses(df)
    return {
        "type": "complete",
        "summary": base,
        "statistical": statistical,
        "predictive": predictive,
        "quality": quality,
        "hypotheses": hypotheses,
    }


# ─────────────────────────────────────────────────────────────────────────────
# CODE GENERATOR — Python, R, SQL, JavaScript
# ─────────────────────────────────────────────────────────────────────────────

def generate_code(language: str, analysis_type: str) -> str:
    """Generate production-ready code templates based on requested language and task."""
    language = language.lower()
    analysis_type = analysis_type.lower()

    if language == "python":
        if analysis_type in ("exploratory", "eda"):
            return '''# ── Python Exploratory Data Analysis ──────────────────────────────────────
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import seaborn as sns

df = pd.read_csv("data_storage/user_behavior_sample.csv")

# 1. Overview
print(f"Shape: {df.shape}")
print("\\nData Types:\\n", df.dtypes)
print("\\nMissing Values:\\n", df.isnull().sum())

# 2. Descriptive statistics
print("\\nSummary Stats:\\n", df.describe(include="all"))

# 3. Correlation heatmap
numeric_df = df.select_dtypes(include=[np.number])
if len(numeric_df.columns) >= 2:
    plt.figure(figsize=(10, 8))
    sns.heatmap(numeric_df.corr(), annot=True, cmap="coolwarm", fmt=".2f",
                linewidths=0.5, square=True)
    plt.title("Correlation Matrix", fontsize=14, fontweight="bold")
    plt.tight_layout()
    plt.savefig("visualizations/correlation_heatmap.png", dpi=150)
    plt.show()

# 4. Distribution plots for numeric columns
for col in numeric_df.columns:
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12, 4))
    sns.histplot(df[col].dropna(), kde=True, ax=ax1, color="#4f8bff")
    ax1.set_title(f"Distribution: {col}")
    sns.boxplot(y=df[col].dropna(), ax=ax2, color="#22c55e")
    ax2.set_title(f"Box Plot: {col}")
    plt.tight_layout()
    plt.savefig(f"visualizations/dist_{col}.png", dpi=150)
    plt.show()
'''
        elif analysis_type == "cleaning":
            return '''# ── Python Data Cleaning Pipeline ──────────────────────────────────────────
import pandas as pd
import numpy as np

df = pd.read_csv("data_storage/user_behavior_sample.csv")
print(f"Before: {df.shape}")

# 1. Drop duplicate rows
df = df.drop_duplicates()

# 2. Impute numeric columns with median (robust to outliers)
numeric_cols = df.select_dtypes(include=[np.number]).columns
for col in numeric_cols:
    if df[col].isnull().any():
        df[col] = df[col].fillna(df[col].median())

# 3. Impute categorical columns with mode
cat_cols = df.select_dtypes(exclude=[np.number]).columns
for col in cat_cols:
    if df[col].isnull().any():
        df[col] = df[col].fillna(df[col].mode()[0])

# 4. Standardise timestamps
if "timestamp" in df.columns:
    df["timestamp"] = pd.to_datetime(df["timestamp"])

# 5. Remove IQR outliers from numeric columns
for col in numeric_cols:
    q1, q3 = df[col].quantile([0.25, 0.75])
    iqr = q3 - q1
    df = df[(df[col] >= q1 - 1.5*iqr) & (df[col] <= q3 + 1.5*iqr)]

print(f"After:  {df.shape}")
df.to_csv("data_storage/user_behavior_cleaned.csv", index=False)
print("✅ Cleaned dataset saved.")
'''
        elif analysis_type in ("visualization", "plotting"):
            return '''# ── Python Visualization Suite ────────────────────────────────────────────
import pandas as pd
import matplotlib.pyplot as plt
import seaborn as sns

sns.set_theme(style="darkgrid", palette="muted")
df = pd.read_csv("data_storage/user_behavior_sample.csv")

fig, axes = plt.subplots(2, 2, figsize=(14, 10))
fig.suptitle("User Behavior Analysis Dashboard", fontsize=16, fontweight="bold")

# 1. Revenue distribution
if "revenue" in df.columns:
    sns.histplot(df["revenue"].dropna(), kde=True, ax=axes[0,0], color="#4f8bff", bins=20)
    axes[0,0].set_title("Revenue Distribution")

# 2. Device type breakdown
if "device_type" in df.columns:
    counts = df["device_type"].value_counts()
    axes[0,1].pie(counts, labels=counts.index, autopct="%1.1f%%",
                  colors=["#4f8bff","#22c55e","#f59e0b","#a855f7"])
    axes[0,1].set_title("Device Type Share")

# 3. Top actions bar chart
if "action" in df.columns:
    top_actions = df["action"].value_counts().head(8)
    sns.barplot(x=top_actions.values, y=top_actions.index, ax=axes[1,0], palette="Blues_r")
    axes[1,0].set_title("Top Actions")

# 4. Revenue by location
if "location" in df.columns and "revenue" in df.columns:
    loc_rev = df.groupby("location")["revenue"].mean().sort_values(ascending=False).head(8)
    sns.barplot(x=loc_rev.values, y=loc_rev.index, ax=axes[1,1], palette="Greens_r")
    axes[1,1].set_title("Avg Revenue by Location")

plt.tight_layout()
plt.savefig("visualizations/dashboard.png", dpi=150)
plt.show()
'''
        else:  # ml
            return '''# ── Python Machine Learning Pipeline ──────────────────────────────────────
import pandas as pd
import numpy as np
from sklearn.model_selection import train_test_split, cross_val_score
from sklearn.preprocessing import LabelEncoder, StandardScaler
from sklearn.ensemble import RandomForestRegressor, GradientBoostingRegressor
from sklearn.linear_model import Ridge
from sklearn.metrics import mean_squared_error, r2_score, mean_absolute_error

df = pd.read_csv("data_storage/user_behavior_sample.csv")

if "revenue" not in df.columns:
    raise ValueError("No 'revenue' column found as prediction target.")

df = df.dropna(subset=["revenue"])
y = df["revenue"]
X = df.drop(columns=["user_id", "session_id", "timestamp", "revenue",
                      "page_url"], errors="ignore")

# Encode categoricals
le = LabelEncoder()
for col in X.select_dtypes(exclude=[np.number]).columns:
    X[col] = le.fit_transform(X[col].astype(str))
X = X.fillna(X.median())

X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42)

models = {
    "Ridge Regression": Ridge(alpha=1.0),
    "Random Forest":    RandomForestRegressor(n_estimators=100, random_state=42),
    "Gradient Boost":   GradientBoostingRegressor(n_estimators=100, random_state=42),
}

for name, model in models.items():
    model.fit(X_train, y_train)
    preds = model.predict(X_test)
    print(f"\\n── {name} ──")
    print(f"  R²  : {r2_score(y_test, preds):.4f}")
    print(f"  RMSE: {np.sqrt(mean_squared_error(y_test, preds)):.4f}")
    print(f"  MAE : {mean_absolute_error(y_test, preds):.4f}")

# Feature importances from Random Forest
rf = models["Random Forest"]
importances = pd.Series(rf.feature_importances_, index=X.columns).sort_values(ascending=False)
print("\\nTop 10 Features:\\n", importances.head(10))
'''

    elif language == "r":
        if analysis_type in ("exploratory", "eda"):
            return '''# ── R Exploratory Data Analysis ──────────────────────────────────────────
library(tidyverse)
library(corrplot)
library(ggplot2)
library(skimr)

df <- read_csv("data_storage/user_behavior_sample.csv")

# 1. Overview
glimpse(df)
skim(df)

# 2. Correlation matrix
numeric_df <- df %>% select(where(is.numeric))
corr_matrix <- cor(numeric_df, use = "pairwise.complete.obs")
corrplot(corr_matrix, method = "color", type = "upper",
         tl.cex = 0.8, addCoef.col = "black", number.cex = 0.7,
         col = colorRampPalette(c("#ef4444", "white", "#22c55e"))(200))

# 3. Revenue distribution
if ("revenue" %in% names(df)) {
  ggplot(df, aes(x = revenue)) +
    geom_histogram(aes(y = ..density..), fill = "#4f8bff", alpha = 0.7, bins = 30) +
    geom_density(color = "#22c55e", size = 1.2) +
    labs(title = "Revenue Distribution", x = "Revenue ($)", y = "Density") +
    theme_dark()
}

# 4. Device type breakdown
if ("device_type" %in% names(df)) {
  df %>%
    count(device_type) %>%
    ggplot(aes(x = reorder(device_type, n), y = n, fill = device_type)) +
    geom_col() +
    coord_flip() +
    labs(title = "Sessions by Device Type", x = "", y = "Count") +
    theme_minimal()
}
'''
        elif analysis_type == "clustering":
            return '''# ── R Clustering Analysis ─────────────────────────────────────────────────
library(tidyverse)
library(cluster)
library(factoextra)

df <- read_csv("data_storage/user_behavior_sample.csv")

# Select and scale numeric features
numeric_data <- df %>%
  select(where(is.numeric)) %>%
  drop_na() %>%
  scale()

# Determine optimal k with elbow method
fviz_nbclust(numeric_data, kmeans, method = "wss") +
  labs(title = "Elbow Method — Optimal K")

# Fit K-Means (k=3 as default)
set.seed(42)
km <- kmeans(numeric_data, centers = 3, nstart = 25)

# Visualise clusters
fviz_cluster(km, data = numeric_data,
             palette = c("#4f8bff", "#22c55e", "#f59e0b"),
             ggtheme = theme_dark(),
             main = "User Segments — K-Means Clustering")

# Silhouette score
sil <- silhouette(km$cluster, dist(numeric_data))
fviz_silhouette(sil) + theme_minimal()
'''
        else:
            return '''# ── R Statistical Analysis ────────────────────────────────────────────────
library(tidyverse)
library(broom)

df <- read_csv("data_storage/user_behavior_sample.csv")

# Summary statistics
df %>% summary()

# Descriptive stats by group
if ("device_type" %in% names(df) && "revenue" %in% names(df)) {
  df %>%
    group_by(device_type) %>%
    summarise(
      n        = n(),
      mean_rev = mean(revenue, na.rm = TRUE),
      sd_rev   = sd(revenue, na.rm = TRUE),
      med_rev  = median(revenue, na.rm = TRUE)
    ) %>%
    arrange(desc(mean_rev))
}

# ANOVA test: does device_type affect revenue?
if ("device_type" %in% names(df) && "revenue" %in% names(df)) {
  aov_model <- aov(revenue ~ device_type, data = df)
  tidy(aov_model) %>% print()
  TukeyHSD(aov_model) %>% tidy() %>% print()
}
'''

    elif language == "sql":
        return '''-- ── SQL Exploratory Queries ───────────────────────────────────────────────
-- Target table: user_behavior_sample

-- 1. Row count
SELECT COUNT(1) AS total_rows FROM user_behavior_sample;

-- 2. Device type distribution
SELECT
  device_type,
  COUNT(1) AS session_count,
  ROUND(COUNT(1) * 100.0 / SUM(COUNT(1)) OVER (), 2) AS pct_share
FROM user_behavior_sample
GROUP BY device_type
ORDER BY session_count DESC;

-- 3. Revenue by location (top 10)
SELECT
  location,
  COUNT(DISTINCT user_id)  AS unique_users,
  COUNT(1)                 AS total_sessions,
  ROUND(SUM(revenue), 2)  AS total_revenue,
  ROUND(AVG(revenue), 2)  AS avg_revenue
FROM user_behavior_sample
GROUP BY location
ORDER BY total_revenue DESC
LIMIT 10;

-- 4. Action funnel analysis
SELECT
  action,
  COUNT(1) AS total,
  ROUND(COUNT(1) * 100.0 / (SELECT COUNT(1) FROM user_behavior_sample), 2) AS funnel_pct
FROM user_behavior_sample
GROUP BY action
ORDER BY total DESC;

-- 5. Revenue cohort by device + location
SELECT
  device_type,
  location,
  ROUND(AVG(revenue), 2) AS avg_revenue,
  COUNT(1)               AS sessions
FROM user_behavior_sample
GROUP BY device_type, location
HAVING sessions > 5
ORDER BY avg_revenue DESC
LIMIT 20;
'''

    else:  # javascript
        return '''// ── JavaScript / Node.js Data Analysis ───────────────────────────────────
const fs   = require("fs");
const path = require("path");

const CSV_PATH = path.join(__dirname, "data_storage", "user_behavior_sample.csv");

// ── Parse CSV helper ──────────────────────────────────────────────────────
function parseCSV(content) {
  const lines   = content.trim().split("\\n");
  const headers = lines[0].split(",").map(h => h.trim());
  return lines.slice(1).map(line => {
    const values = line.split(",");
    return Object.fromEntries(headers.map((h, i) => [h, values[i]?.trim() ?? ""]));
  });
}

// ── Load and analyse ──────────────────────────────────────────────────────
const raw  = fs.readFileSync(CSV_PATH, "utf-8");
const data = parseCSV(raw);

console.log(`Total records: ${data.length}`);

// Revenue aggregation
const revenues = data.map(r => parseFloat(r.revenue)).filter(v => !isNaN(v));
const totalRev  = revenues.reduce((a, b) => a + b, 0);
const avgRev    = totalRev / revenues.length;
const maxRev    = Math.max(...revenues);

console.log(`Total Revenue : $${totalRev.toFixed(2)}`);
console.log(`Average Revenue: $${avgRev.toFixed(2)}`);
console.log(`Max Revenue    : $${maxRev.toFixed(2)}`);

// Device type breakdown
const deviceCounts = {};
data.forEach(r => {
  deviceCounts[r.device_type] = (deviceCounts[r.device_type] ?? 0) + 1;
});
console.log("\\nDevice Breakdown:");
Object.entries(deviceCounts)
  .sort((a, b) => b[1] - a[1])
  .forEach(([d, c]) => console.log(`  ${d}: ${c} sessions (${(c / data.length * 100).toFixed(1)}%)`));
'''


# ─────────────────────────────────────────────────────────────────────────────
# REPORT WRITER — executive Markdown report
# ─────────────────────────────────────────────────────────────────────────────

def generate_report(df: pd.DataFrame, filename: str) -> str:
    """Compile a professional Markdown Executive Report describing the dataset."""
    row_count = len(df)
    col_count = len(df.columns)

    total_rev = float(df["revenue"].sum()) if "revenue" in df.columns else 0.0
    top_dev   = str(df["device_type"].mode().iloc[0])  if "device_type" in df.columns and not df["device_type"].empty else "N/A"
    top_loc   = str(df["location"].mode().iloc[0])     if "location"    in df.columns and not df["location"].empty    else "N/A"
    top_act   = str(df["action"].mode().iloc[0])       if "action"      in df.columns and not df["action"].empty      else "N/A"

    timestamp_range = "N/A"
    if "timestamp" in df.columns:
        try:
            dates = pd.to_datetime(df["timestamp"]).dropna()
            if not dates.empty:
                timestamp_range = f"{dates.min().strftime('%Y-%m-%d %H:%M')} → {dates.max().strftime('%Y-%m-%d %H:%M')}"
        except Exception:
            pass

    # Quality snapshot
    null_total = int(df.isnull().sum().sum())
    dup_count  = int(df.duplicated().sum())
    completeness = round((1 - null_total / (row_count * col_count)) * 100, 1) if (row_count * col_count) > 0 else 100.0

    report = f"""# EXECUTIVE DATA ANALYSIS REPORT
**Dataset:** `{filename}`
**Report Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} (UTC)
**Status:** Completed ✅

---

## 1. Dataset Overview

| Metric | Value |
| :--- | :--- |
| **Total Rows** | {row_count:,} |
| **Total Columns** | {col_count} |
| **Timestamp Range** | {timestamp_range} |
| **Total Revenue** | ${total_rev:,.2f} |
| **Primary Device** | {top_dev} |
| **Primary Location** | {top_loc} |
| **Primary Action** | {top_act} |
| **Completeness** | {completeness}% |
| **Duplicate Rows** | {dup_count} |

---

## 2. Data Quality Summary

- **Missing Cells:** {null_total:,} across all columns
- **Duplicate Records:** {dup_count}
- **Completeness Rate:** {completeness}%

{f'> ⚠️ **{null_total} null values detected.** Consider imputation before modeling.' if null_total > 0 else '> ✅ No missing values detected.'}
{f'> ⚠️ **{dup_count} duplicate rows found.** Recommend deduplication.' if dup_count > 0 else ''}

---

## 3. Column Inventory

| # | Column | Type | Null % | Unique | Highlights |
| :-: | :--- | :-: | :-: | :-: | :--- |
"""
    for i, col in enumerate(df.columns, 1):
        null_count   = int(df[col].isnull().sum())
        null_pct     = round(null_count / row_count * 100, 1) if row_count else 0.0
        unique_count = int(df[col].nunique())
        dtype        = str(df[col].dtype)
        highlights   = "—"
        if np.issubdtype(df[col].dtype, np.number):
            mean_val = df[col].mean()
            if not pd.isna(mean_val):
                highlights = f"Mean: {mean_val:.2f} | Range: [{df[col].min():.1f}, {df[col].max():.1f}]"
        else:
            if unique_count > 0:
                top_val = df[col].mode().iloc[0] if not df[col].empty else "N/A"
                highlights = f"Mode: '{top_val}' ({df[col].value_counts().iloc[0]}×)"
        report += f"| {i} | **{col}** | `{dtype}` | {null_pct}% | {unique_count:,} | {highlights} |\n"

    report += f"""
---

## 4. Key Observations

1. **Volume:** {row_count:,} records across {col_count} features — sufficient for statistical analysis.
2. **Device Preference:** `{top_dev}` is the dominant access channel.
3. **Geographic Concentration:** `{top_loc}` has the highest session density.
4. **Revenue Performance:** Cumulative revenue of **${total_rev:,.2f}** indicates {('strong' if total_rev > 10000 else 'moderate')} conversion.
5. **Data Integrity:** {completeness}% completeness{(' with minor gaps requiring attention.' if null_total > 0 else ' — dataset is clean.')}

---

## 5. Recommended Next Steps

- [ ] Run **Quality Assurance** check for outlier detection and missingness analysis
- [ ] Execute **Statistical Analysis** to identify significant correlations
- [ ] Apply **Predictive Modeling** to estimate feature importance for revenue
- [ ] Generate **Hypotheses** for A/B testing or further investigation

---

*Report compiled automatically by Claude Data Analysis Assistant · {datetime.now().strftime('%Y-%m-%d')}*
"""
    return report
