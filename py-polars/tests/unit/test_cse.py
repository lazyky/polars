import re
from datetime import date, datetime
from tempfile import NamedTemporaryFile
from typing import Any

import numpy as np
import pytest

import polars as pl
from polars.testing import assert_frame_equal


# https://github.com/pola-rs/polars/issues/5405
def test_cse_rename_cross_join_5405() -> None:
    right = pl.DataFrame({"A": [1, 2], "B": [3, 4], "D": [5, 6]}).lazy()
    left = pl.DataFrame({"C": [3, 4]}).lazy().join(right.select("A"), how="cross")

    result = left.join(right.rename({"B": "C"}), on=["A", "C"], how="left").collect(
        comm_subplan_elim=True
    )

    expected = pl.DataFrame(
        {
            "C": [3, 3, 4, 4],
            "A": [1, 2, 1, 2],
            "D": [5, None, None, 6],
        }
    )
    assert_frame_equal(result, expected)


def test_union_duplicates() -> None:
    n_dfs = 10
    df_lazy = pl.DataFrame({}).lazy()
    lazy_dfs = [df_lazy for _ in range(n_dfs)]

    result = len(
        re.findall(
            r".*CACHE\[id: .*, count: 9].*",
            pl.concat(lazy_dfs).explain(),
            flags=re.MULTILINE,
        )
    )
    assert result


# https://github.com/pola-rs/polars/issues/11116
def test_cse_with_struct_expr_11116() -> None:
    df = pl.DataFrame([{"s": {"a": 1, "b": 4}, "c": 3}]).lazy()

    result = df.with_columns(
        pl.col("s").struct.field("a").alias("s_a"),
        pl.col("s").struct.field("b").alias("s_b"),
        (
            (pl.col("s").struct.field("a") <= pl.col("c"))
            & (pl.col("s").struct.field("b") > pl.col("c"))
        ).alias("c_between_a_and_b"),
    ).collect(comm_subexpr_elim=True)

    expected = pl.DataFrame(
        {
            "s": [{"a": 1, "b": 4}],
            "c": [3],
            "s_a": [1],
            "s_b": [4],
            "c_between_a_and_b": [True],
        }
    )
    assert_frame_equal(result, expected)


# https://github.com/pola-rs/polars/issues/6081
def test_cse_schema_6081() -> None:
    df = pl.DataFrame(
        data=[
            [date(2022, 12, 12), 1, 1],
            [date(2022, 12, 12), 1, 2],
            [date(2022, 12, 13), 5, 2],
        ],
        schema=["date", "id", "value"],
        orient="row",
    ).lazy()

    min_value_by_group = df.group_by(["date", "id"]).agg(
        pl.col("value").min().alias("min_value")
    )

    result = df.join(min_value_by_group, on=["date", "id"], how="left").collect(
        comm_subplan_elim=True, projection_pushdown=True
    )
    expected = pl.DataFrame(
        {
            "date": [date(2022, 12, 12), date(2022, 12, 12), date(2022, 12, 13)],
            "id": [1, 1, 5],
            "value": [1, 2, 2],
            "min_value": [1, 1, 2],
        }
    )
    assert_frame_equal(result, expected)


def test_cse_9630() -> None:
    lf1 = pl.LazyFrame({"key": [1], "x": [1]})
    lf2 = pl.LazyFrame({"key": [1], "y": [2]})

    joined_lf2 = lf1.join(lf2, on="key")

    all_subsections = (
        pl.concat(
            [
                lf1.select("key", pl.col("x").alias("value")),
                joined_lf2.select("key", pl.col("y").alias("value")),
            ]
        )
        .group_by("key")
        .agg(pl.col("value"))
    )

    intersected_df1 = all_subsections.join(lf1, on="key")
    intersected_df2 = all_subsections.join(lf2, on="key")

    result = intersected_df1.join(intersected_df2, on=["key"], how="left").collect(
        comm_subplan_elim=True
    )

    expected = pl.DataFrame(
        {
            "key": [1],
            "value": [[1, 2]],
            "x": [1],
            "value_right": [[1, 2]],
            "y": [2],
        }
    )
    assert_frame_equal(result, expected)


@pytest.mark.write_disk()
def test_schema_row_index_cse() -> None:
    csv_a = NamedTemporaryFile()
    csv_a.write(
        b"""
A,B
Gr1,A
Gr1,B
    """.strip()
    )
    csv_a.seek(0)

    df_a = pl.scan_csv(csv_a.name).with_row_index("Idx")

    result = (
        df_a.join(df_a, on="B")
        .group_by("A", maintain_order=True)
        .all()
        .collect(comm_subexpr_elim=True)
    )

    csv_a.close()

    expected = pl.DataFrame(
        {
            "A": ["Gr1"],
            "Idx": [[0, 1]],
            "B": [["A", "B"]],
            "Idx_right": [[0, 1]],
            "A_right": [["Gr1", "Gr1"]],
        },
        schema_overrides={"Idx": pl.List(pl.UInt32), "Idx_right": pl.List(pl.UInt32)},
    )
    assert_frame_equal(result, expected)


def test_cse_expr_selection_context(monkeypatch: Any, capfd: Any) -> None:
    monkeypatch.setenv("POLARS_VERBOSE", "1")
    q = pl.LazyFrame(
        {
            "a": [1, 2, 3, 4],
            "b": [1, 2, 3, 4],
            "c": [1, 2, 3, 4],
        }
    )

    derived = (pl.col("a") * pl.col("b")).sum()
    derived2 = derived * derived

    exprs = [
        derived.alias("d1"),
        (derived * pl.col("c").sum() - 1).alias("foo"),
        derived2.alias("d2"),
        (derived2 * 10).alias("d3"),
    ]

    result = q.select(exprs).collect(comm_subexpr_elim=True)
    expected = pl.DataFrame(
        {
            "d1": [30],
            "foo": [299],
            "d2": [900],
            "d3": [9000],
        }
    )
    assert_frame_equal(result, expected)

    result = q.with_columns(exprs).collect(comm_subexpr_elim=True)
    expected = pl.DataFrame(
        {
            "a": [1, 2, 3, 4],
            "b": [1, 2, 3, 4],
            "c": [1, 2, 3, 4],
            "d1": [30, 30, 30, 30],
            "foo": [299, 299, 299, 299],
            "d2": [900, 900, 900, 900],
            "d3": [9000, 9000, 9000, 9000],
        }
    )
    assert_frame_equal(result, expected)

    out = capfd.readouterr().out
    assert "run ProjectionExec with 2 CSE" in out
    assert "run StackExec with 2 CSE" in out


def test_windows_cse_excluded() -> None:
    lf = pl.LazyFrame(
        data=[
            ("a", "aaa", 1),
            ("a", "bbb", 3),
            ("a", "ccc", 1),
            ("c", "xxx", 2),
            ("c", "yyy", 3),
            ("c", "zzz", 4),
            ("b", "qqq", 0),
        ],
        schema=["a", "b", "c"],
    )

    result = lf.select(
        c_diff=pl.col("c").diff(1),
        c_diff_by_a=pl.col("c").diff(1).over("a"),
    ).collect(comm_subexpr_elim=True)

    expected = pl.DataFrame(
        {
            "c_diff": [None, 2, -2, 1, 1, 1, -4],
            "c_diff_by_a": [None, 2, -2, None, 1, 1, None],
        }
    )
    assert_frame_equal(result, expected)


def test_cse_group_by_10215() -> None:
    lf = pl.LazyFrame({"a": [1], "b": [1]})

    result = lf.group_by("b").agg(
        (pl.col("a").sum() * pl.col("a").sum()).alias("x"),
        (pl.col("b").sum() * pl.col("b").sum()).alias("y"),
        (pl.col("a").sum() * pl.col("a").sum()).alias("x2"),
        ((pl.col("a") + 2).sum() * pl.col("a").sum()).alias("x3"),
        ((pl.col("a") + 2).sum() * pl.col("b").sum()).alias("x4"),
        ((pl.col("a") + 2).sum() * pl.col("b").sum()),
    )

    assert "__POLARS_CSER" in result.explain(comm_subexpr_elim=True)
    expected = pl.DataFrame(
        {
            "b": [1],
            "x": [1],
            "y": [1],
            "x2": [1],
            "x3": [3],
            "x4": [3],
            "a": [3],
        }
    )
    assert_frame_equal(result.collect(comm_subexpr_elim=True), expected)


def test_cse_mixed_window_functions() -> None:
    # checks if the window caches are cleared
    # there are windows in the cse's and the default expressions
    lf = pl.LazyFrame({"a": [1], "b": [1], "c": [1]})

    result = lf.select(
        pl.col("a"),
        pl.col("b"),
        pl.col("c"),
        pl.col("b").rank().alias("rank"),
        pl.col("b").rank().alias("d_rank"),
        pl.col("b").first().over([pl.col("a")]).alias("b_first"),
        pl.col("b").last().over([pl.col("a")]).alias("b_last"),
        pl.col("b").shift().alias("b_lag_1"),
        pl.col("b").shift().alias("b_lead_1"),
        pl.col("c").cum_sum().alias("c_cumsum"),
        pl.col("c").cum_sum().over([pl.col("a")]).alias("c_cumsum_by_a"),
        pl.col("c").diff().alias("c_diff"),
        pl.col("c").diff().over([pl.col("a")]).alias("c_diff_by_a"),
    ).collect(comm_subexpr_elim=True)

    expected = pl.DataFrame(
        {
            "a": [1],
            "b": [1],
            "c": [1],
            "rank": [1.0],
            "d_rank": [1.0],
            "b_first": [1],
            "b_last": [1],
            "b_lag_1": [None],
            "b_lead_1": [None],
            "c_cumsum": [1],
            "c_cumsum_by_a": [1],
            "c_diff": [None],
            "c_diff_by_a": [None],
        },
    ).with_columns(pl.col(pl.Null).cast(pl.Int64))
    assert_frame_equal(result, expected)


def test_cse_10401() -> None:
    df = pl.LazyFrame({"clicks": [1.0, float("nan"), None]})

    q = df.with_columns(pl.all().fill_null(0).fill_nan(0))

    assert r"""col("clicks").fill_null([0]).alias("__POLARS_CSER""" in q.explain()

    expected = pl.DataFrame({"clicks": [1.0, 0.0, 0.0]})
    assert_frame_equal(q.collect(comm_subexpr_elim=True), expected)


def test_cse_10441() -> None:
    lf = pl.LazyFrame({"a": [1, 2, 3], "b": [3, 2, 1]})

    result = lf.select(
        pl.col("a").sum() + pl.col("a").sum() + pl.col("b").sum()
    ).collect(comm_subexpr_elim=True)

    expected = pl.DataFrame({"a": [18]})
    assert_frame_equal(result, expected)


def test_cse_10452() -> None:
    lf = pl.LazyFrame({"a": [1, 2, 3], "b": [3, 2, 1]})
    q = lf.select(
        pl.col("b").sum() + pl.col("a").sum().over(pl.col("b")) + pl.col("b").sum()
    )

    assert "__POLARS_CSE" in q.explain(comm_subexpr_elim=True)

    expected = pl.DataFrame({"b": [13, 14, 15]})
    assert_frame_equal(q.collect(comm_subexpr_elim=True), expected)


def test_cse_group_by_ternary_10490() -> None:
    lf = pl.LazyFrame(
        {
            "a": [1, 1, 2, 2],
            "b": [1, 2, 3, 4],
            "c": [2, 3, 4, 5],
        }
    )

    result = (
        lf.group_by("a")
        .agg(
            [
                pl.when(pl.col(col).is_null().all()).then(None).otherwise(1).alias(col)
                for col in ["b", "c"]
            ]
            + [
                (pl.col("a").sum() * pl.col("a").sum()).alias("x"),
                (pl.col("b").sum() * pl.col("b").sum()).alias("y"),
                (pl.col("a").sum() * pl.col("a").sum()).alias("x2"),
                ((pl.col("a") + 2).sum() * pl.col("a").sum()).alias("x3"),
                ((pl.col("a") + 2).sum() * pl.col("b").sum()).alias("x4"),
            ]
        )
        .collect(comm_subexpr_elim=True)
        .sort("a")
    )

    expected = pl.DataFrame(
        {
            "a": [1, 2],
            "b": [1, 1],
            "c": [1, 1],
            "x": [4, 16],
            "y": [9, 49],
            "x2": [4, 16],
            "x3": [12, 32],
            "x4": [18, 56],
        },
        schema_overrides={"b": pl.Int32, "c": pl.Int32},
    )
    assert_frame_equal(result, expected)


def test_cse_quantile_10815() -> None:
    np.random.seed(1)
    a = np.random.random(10)
    b = np.random.random(10)
    df = pl.DataFrame({"a": a, "b": b})
    cols = ["a", "b"]
    q = df.lazy().select(
        *(
            pl.col(c).quantile(0.75, interpolation="midpoint").name.suffix("_3")
            for c in cols
        ),
        *(
            pl.col(c).quantile(0.25, interpolation="midpoint").name.suffix("_1")
            for c in cols
        ),
    )
    assert "__POLARS_CSE" not in q.explain()
    assert q.collect().to_dict(as_series=False) == {
        "a_3": [0.40689473946662197],
        "b_3": [0.6145786693120769],
        "a_1": [0.16650805109739197],
        "b_1": [0.2012768694081981],
    }


def test_cse_nan_10824() -> None:
    v = pl.col("a") / pl.col("b")
    magic = pl.when(v > 0).then(pl.lit(float("nan"))).otherwise(v)
    assert (
        str(
            (
                pl.DataFrame(
                    {
                        "a": [1.0],
                        "b": [1.0],
                    }
                )
                .lazy()
                .select(magic)
                .collect(comm_subexpr_elim=True)
            ).to_dict(as_series=False)
        )
        == "{'literal': [nan]}"
    )


def test_cse_10901() -> None:
    df = pl.DataFrame(data=range(6), schema={"a": pl.Int64})
    a = pl.col("a").rolling_sum(window_size=2)
    b = pl.col("a").rolling_sum(window_size=3)
    exprs = {
        "ax1": a,
        "ax2": a * 2,
        "bx1": b,
        "bx2": b * 2,
    }

    expected = pl.DataFrame(
        {
            "a": [0, 1, 2, 3, 4, 5],
            "ax1": [None, 1, 3, 5, 7, 9],
            "ax2": [None, 2, 6, 10, 14, 18],
            "bx1": [None, None, 3, 6, 9, 12],
            "bx2": [None, None, 6, 12, 18, 24],
        }
    )

    assert_frame_equal(df.lazy().with_columns(**exprs).collect(), expected)


def test_cse_count_in_group_by() -> None:
    q = (
        pl.LazyFrame({"a": [1, 1, 2], "b": [1, 2, 3], "c": [40, 51, 12]})
        .group_by("a")
        .agg(pl.all().slice(0, pl.len() - 1))
    )

    assert "POLARS_CSER" not in q.explain()
    assert q.collect().sort("a").to_dict(as_series=False) == {
        "a": [1, 2],
        "b": [[1], []],
        "c": [[40], []],
    }


def test_no_cse_in_with_context() -> None:
    df1 = pl.DataFrame(
        {
            "timestamp": [
                datetime(2023, 1, 1, 0, 0),
                datetime(2023, 5, 1, 0, 0),
                datetime(2023, 10, 1, 0, 0),
            ],
            "value": [2, 5, 9],
        }
    )
    df2 = pl.DataFrame(
        {
            "date_start": [
                datetime(2022, 12, 31, 0, 0),
                datetime(2023, 1, 2, 0, 0),
            ],
            "date_end": [
                datetime(2023, 4, 30, 0, 0),
                datetime(2023, 5, 5, 0, 0),
            ],
            "label": [0, 1],
        }
    )

    assert (
        df1.lazy()
        .with_context(df2.lazy())
        .select(
            pl.col("date_start", "label").gather(
                pl.col("date_start").search_sorted("timestamp") - 1
            ),
        )
    ).collect().to_dict(as_series=False) == {
        "date_start": [
            datetime(2022, 12, 31, 0, 0),
            datetime(2023, 1, 2, 0, 0),
            datetime(2023, 1, 2, 0, 0),
        ],
        "label": [0, 1, 1],
    }


def test_cse_slice_11594() -> None:
    df = pl.LazyFrame({"a": [1, 2, 1, 2, 1, 2]})

    q = df.select(
        pl.col("a").slice(offset=1, length=pl.len() - 1).alias("1"),
        pl.col("a").slice(offset=1, length=pl.len() - 1).alias("2"),
    )

    assert "__POLARS_CSE" in q.explain(comm_subexpr_elim=True)

    assert q.collect(comm_subexpr_elim=True).to_dict(as_series=False) == {
        "1": [2, 1, 2, 1, 2],
        "2": [2, 1, 2, 1, 2],
    }

    q = df.select(
        pl.col("a").slice(offset=1, length=pl.len() - 1).alias("1"),
        pl.col("a").slice(offset=0, length=pl.len() - 1).alias("2"),
    )

    assert "__POLARS_CSE" in q.explain(comm_subexpr_elim=True)

    assert q.collect(comm_subexpr_elim=True).to_dict(as_series=False) == {
        "1": [2, 1, 2, 1, 2],
        "2": [1, 2, 1, 2, 1],
    }


def test_cse_is_in_11489() -> None:
    df = pl.DataFrame(
        {"cond": [1, 2, 3, 2, 1], "x": [1.0, 0.20, 3.0, 4.0, 0.50]}
    ).lazy()
    any_cond = (
        pl.when(pl.col("cond").is_in([2, 3]))
        .then(True)
        .when(pl.col("cond").is_in([1]))
        .then(False)
        .otherwise(None)
        .alias("any_cond")
    )
    val = (
        pl.when(any_cond)
        .then(1.0)
        .when(~any_cond)
        .then(0.0)
        .otherwise(None)
        .alias("val")
    )
    assert df.select("cond", any_cond, val).collect().to_dict(as_series=False) == {
        "cond": [1, 2, 3, 2, 1],
        "any_cond": [False, True, True, True, False],
        "val": [0.0, 1.0, 1.0, 1.0, 0.0],
    }


def test_cse_11958() -> None:
    df = pl.LazyFrame({"a": [1, 2, 3, 4, 5]})
    vector_losses = []
    for lag in range(1, 5):
        difference = pl.col("a") - pl.col("a").shift(lag)
        component_loss = pl.when(difference >= 0).then(difference * 10)
        vector_losses.append(component_loss.alias(f"diff{lag}"))

    q = df.select(vector_losses)
    assert "__POLARS_CSE" in q.explain(comm_subexpr_elim=True)
    assert q.collect(comm_subexpr_elim=True).to_dict(as_series=False) == {
        "diff1": [None, 10, 10, 10, 10],
        "diff2": [None, None, 20, 20, 20],
        "diff3": [None, None, None, 30, 30],
        "diff4": [None, None, None, None, 40],
    }
