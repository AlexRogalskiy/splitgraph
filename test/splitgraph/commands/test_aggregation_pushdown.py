import json
from decimal import Decimal
from test.splitgraph.conftest import _mount_elasticsearch

import pytest
import yaml

from splitgraph.engine import ResultShape, get_engine


def _extract_queries_from_explain(result):
    queries = []
    query_str = ""

    for o in result:
        if query_str != "":
            query_str += o[0] + "\n"
        elif "Multicorn: Query:" in o[0]:
            query_str = "{"

        if o == ("}",):
            queries.append(json.loads(query_str))
            query_str = ""

    return queries


_bare_sequential_scan = {"query": {"bool": {"must": []}}}
_bare_filtering_query = {"query": {"bool": {"must": [{"range": {"age": {"gt": 30}}}]}}}


@pytest.mark.mounting
def test_elasticsearch_aggregation_functions_only(local_engine_empty):
    _mount_elasticsearch()

    query = """
    SELECT max(account_number), avg(balance), max(balance),
        sum(balance), min(age), avg(age)
    FROM es.account
    """

    # Ensure query is going to be aggregated on the foreign server
    result = get_engine().run_sql("EXPLAIN " + query)
    assert _extract_queries_from_explain(result)[0] == {
        "query": {"bool": {"must": []}},
        "aggs": {
            "max.account_number": {"max": {"field": "account_number"}},
            "avg.balance": {"avg": {"field": "balance"}},
            "max.balance": {"max": {"field": "balance"}},
            "sum.balance": {"sum": {"field": "balance"}},
            "min.age": {"min": {"field": "age"}},
            "avg.age": {"avg": {"field": "age"}},
        },
    }

    # Ensure results are correct
    result = get_engine().run_sql(query)
    assert len(result) == 1

    # Assert aggregation result
    assert result[0] == (999, 25714.837, 49989, 25714837, 20, Decimal("30.171"))


@pytest.mark.mounting
def test_elasticsearch_gropuing_clauses_only(snapshot, local_engine_empty):
    _mount_elasticsearch()

    # Single column grouping
    query = "SELECT state FROM es.account GROUP BY state"

    # Ensure grouping is going to be pushed down
    result = get_engine().run_sql("EXPLAIN " + query)
    assert _extract_queries_from_explain(result)[0] == {
        "query": {"bool": {"must": []}},
        "aggs": {
            "group_buckets": {
                "composite": {"sources": [{"state": {"terms": {"field": "state"}}}], "size": 5}
            }
        },
    }

    # Ensure results are correct
    result = get_engine().run_sql(query, return_shape=ResultShape.MANY_ONE)
    assert len(result) == 51

    # Assert aggregation result
    snapshot.assert_match(yaml.dump(result), "account_states.yml")

    # Multi-column grouping
    query = "SELECT gender, age FROM es.account GROUP BY age, gender"

    # Ensure grouping is going to be pushed down
    result = get_engine().run_sql("EXPLAIN " + query)
    assert _extract_queries_from_explain(result)[0] == {
        "query": {"bool": {"must": []}},
        "aggs": {
            "group_buckets": {
                "composite": {
                    "sources": [
                        {"gender": {"terms": {"field": "gender"}}},
                        {"age": {"terms": {"field": "age"}}},
                    ],
                    "size": 5,
                }
            }
        },
    }

    # Ensure results are correct
    result = get_engine().run_sql(query)
    assert len(result) == 42

    # Assert aggregation result
    snapshot.assert_match(yaml.dump(result), "account_genders_and_ages.yml")


@pytest.mark.mounting
def test_elasticsearch_grouping_and_aggregations_bare(snapshot, local_engine_empty):
    _mount_elasticsearch()

    # Aggregations functions and grouping bare combination
    query = "SELECT gender, avg(balance), avg(age) FROM es.account GROUP BY gender"

    # Ensure query is going to be pushed down
    result = get_engine().run_sql("EXPLAIN " + query)
    assert _extract_queries_from_explain(result)[0] == {
        "query": {"bool": {"must": []}},
        "aggs": {
            "group_buckets": {
                "composite": {"sources": [{"gender": {"terms": {"field": "gender"}}}], "size": 5},
                "aggregations": {
                    "avg.balance": {"avg": {"field": "balance"}},
                    "avg.age": {"avg": {"field": "age"}},
                },
            }
        },
    }

    # Ensure results are correct
    result = get_engine().run_sql(query)
    assert len(result) == 2

    # Assert aggregation result
    assert result == [
        ("F", 25623.34685598377, Decimal("30.3184584178499")),
        ("M", 25803.800788954635, Decimal("30.027613412228796")),
    ]

    # We support pushing down aggregation queries with sorting, with the caveat
    # that the sorting operation is performed on the PG side for now
    query = "SELECT age, COUNT(account_number), min(balance) FROM es.account GROUP BY age ORDER BY age DESC"

    # Ensure query is going to be pushed down
    result = get_engine().run_sql("EXPLAIN " + query)
    assert _extract_queries_from_explain(result)[0] == {
        "query": {"bool": {"must": []}},
        "aggs": {
            "group_buckets": {
                "composite": {"sources": [{"age": {"terms": {"field": "age"}}}], "size": 5},
                "aggregations": {
                    "count.account_number": {"value_count": {"field": "account_number"}},
                    "min.balance": {"min": {"field": "balance"}},
                },
            }
        },
    }

    # Ensure results are correct
    result = get_engine().run_sql(query)
    assert len(result) == 21

    # Assert aggregation result
    snapshot.assert_match(yaml.dump(result), "account_count_by_age.yml")


@pytest.mark.mounting
def test_elasticsearch_agg_subquery_pushdown(local_engine_empty):
    """
    Most of the magic in these examples is coming from PG, not our Multicorn code
    (i.e. discarding redundant targets from subqueries).
    Here we just make sure that we don't break that somehow.
    """

    _mount_elasticsearch()

    # DISTINCT on a grouping clause from a subquery
    query = """
    SELECT DISTINCT gender FROM(
        SELECT state, gender, min(age), max(balance)
        FROM es.account GROUP BY state, gender
    ) AS t
    """

    # Ensure only the relevant part is pushed down (i.e. no aggregations as they are redundant)
    result = get_engine().run_sql("EXPLAIN " + query)
    assert _extract_queries_from_explain(result)[0] == {
        "query": {"bool": {"must": []}},
        "aggs": {
            "group_buckets": {
                "composite": {
                    "sources": [
                        {"state": {"terms": {"field": "state"}}},
                        {"gender": {"terms": {"field": "gender"}}},
                    ],
                    "size": 5,
                }
            }
        },
    }

    # Ensure results are correct
    result = get_engine().run_sql(query, return_shape=ResultShape.MANY_ONE)
    assert len(result) == 2

    # Assert aggregation result
    assert result == ["F", "M"]

    # DISTINCT on a aggregated column from a subquery
    query = """
    SELECT DISTINCT min FROM(
        SELECT state, gender, min(age), max(balance)
        FROM es.account GROUP BY state, gender
    ) AS t
    """

    # Ensure only the relevant part is pushed down (no redundant aggregations, i.e. only min)
    result = get_engine().run_sql("EXPLAIN " + query)
    assert _extract_queries_from_explain(result)[0] == {
        "query": {"bool": {"must": []}},
        "aggs": {
            "group_buckets": {
                "composite": {
                    "sources": [
                        {"state": {"terms": {"field": "state"}}},
                        {"gender": {"terms": {"field": "gender"}}},
                    ],
                    "size": 5,
                },
                "aggregations": {"min.age": {"min": {"field": "age"}}},
            }
        },
    }

    # Ensure results are correct
    result = get_engine().run_sql(query, return_shape=ResultShape.MANY_ONE)
    assert len(result) == 8

    # Assert aggregation result
    assert result == [20, 21, 22, 23, 24, 25, 26, 28]

    # Aggregation of the sub-aggregation through a CTE
    query = """
    WITH sub_agg AS (
        SELECT state, gender, min(age), max(balance) as max_balance
        FROM es.account GROUP BY state, gender
    )
    SELECT min(max_balance), gender FROM sub_agg GROUP BY gender
    """

    # Only the subquery is pushed-down, with no redundant aggregations
    result = get_engine().run_sql("EXPLAIN " + query)
    assert _extract_queries_from_explain(result)[0] == {
        "query": {"bool": {"must": []}},
        "aggs": {
            "group_buckets": {
                "composite": {
                    "sources": [
                        {"state": {"terms": {"field": "state"}}},
                        {"gender": {"terms": {"field": "gender"}}},
                    ],
                    "size": 5,
                },
                "aggregations": {"max.balance": {"max": {"field": "balance"}}},
            }
        },
    }

    # Ensure results are correct
    result = get_engine().run_sql(query)
    assert len(result) == 2

    # Assert aggregation result
    assert result == [(37358, "M"), (31968, "F")]


@pytest.mark.mounting
def test_elasticsearch_aggregations_join_combinations(snapshot, local_engine_empty):
    # Sub-aggregations in a join are pushed down
    query = """
    SELECT t1.*, t2.min FROM (
        SELECT age, max(balance) as max
        FROM es.account
        GROUP BY age
    ) AS t1
    JOIN (
        SELECT age, min(balance) as min
        FROM es.account
        GROUP BY age
    ) AS t2
    ON t1.age = t2.age
    """

    # Only the subquery is pushed-down, with no redundant aggregations
    result = get_engine().run_sql("EXPLAIN " + query)
    queries = _extract_queries_from_explain(result)

    assert queries[0] == {
        "query": {"bool": {"must": []}},
        "aggs": {
            "group_buckets": {
                "composite": {
                    "sources": [{"age": {"terms": {"field": "age"}}}],
                    "size": 5,
                },
                "aggregations": {"max.balance": {"max": {"field": "balance"}}},
            }
        },
    }
    assert queries[1] == {
        "query": {"bool": {"must": []}},
        "aggs": {
            "group_buckets": {
                "composite": {
                    "sources": [{"age": {"terms": {"field": "age"}}}],
                    "size": 5,
                },
                "aggregations": {"min.balance": {"min": {"field": "balance"}}},
            }
        },
    }

    # Ensure results are correct
    result = get_engine().run_sql(query)
    assert len(result) == 21

    # Assert aggregation result
    snapshot.assert_match(yaml.dump(result), "account_join_sub_aggs.yml")

    # However, aggregation of a joined table are not pushed down
    result = get_engine().run_sql(
        """
        EXPLAIN SELECT t.state, AVG(t.balance) FROM (
            SELECT l.state AS state, l.balance + r.balance AS balance
            FROM es.account l
            JOIN es.account r USING(state)
        ) t GROUP BY state
        """
    )
    queries = _extract_queries_from_explain(result)
    assert queries[0] == _bare_sequential_scan
    assert queries[1] == _bare_sequential_scan


@pytest.mark.mounting
def test_elasticsearch_not_pushed_down(local_engine_empty):
    _mount_elasticsearch()

    # COUNT STAR is not going to be pushed down
    result = get_engine().run_sql("EXPLAIN SELECT COUNT(*) FROM es.account")
    assert _extract_queries_from_explain(result)[0] == _bare_sequential_scan

    # COUNT DISTINCT queries are not going to be pushed down
    result = get_engine().run_sql("EXPLAIN SELECT COUNT(DISTINCT city) FROM es.account")
    assert _extract_queries_from_explain(result)[0] == _bare_sequential_scan

    # SUM DISTINCT queries are not going to be pushed down
    result = get_engine().run_sql("EXPLAIN SELECT SUM(DISTINCT age) FROM es.account")
    assert _extract_queries_from_explain(result)[0] == _bare_sequential_scan

    # AVG DISTINCT queries are not going to be pushed down
    result = get_engine().run_sql("EXPLAIN SELECT AVG(DISTINCT balance) FROM es.account")
    assert _extract_queries_from_explain(result)[0] == _bare_sequential_scan

    # Queries with proper HAVING are not goint to be pushed down
    result = get_engine().run_sql(
        "EXPLAIN SELECT max(balance) FROM es.account HAVING max(balance) > 30"
    )
    assert _extract_queries_from_explain(result)[0] == _bare_sequential_scan

    # Aggregation with a nested expression won't be pushed down
    result = get_engine().run_sql(
        "EXPLAIN SELECT avg(age * balance) FROM es.account GROUP BY state"
    )
    assert _extract_queries_from_explain(result)[0] == _bare_sequential_scan
