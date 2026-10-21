import pytest

from app.config import DATA_DIR
from app.db import get_store
from app.rag.retriever import AgenticRetriever, get_retriever
from app.rag.vectorstore import InMemoryVectorStore, load_runbook_chunks

EXPECTED = {"n2-transport": "RB-TRN-001", "upf-overload": "RB-UPF-002",
            "ims-cx": "RB-IMS-003", "epc-s6a": "RB-EPC-004"}


@pytest.mark.parametrize("scenario,runbook", EXPECTED.items())
def test_retrieves_the_right_runbook(scenario, runbook):
    store = get_store()
    alarms = store.query("SELECT * FROM alarms WHERE scenario = ?", (scenario,))
    types = [store.node(a["node_id"])["type"] for a in alarms]
    result = get_retriever().retrieve(alarms, types)
    assert result.best_runbook == runbook


def test_chunks_keep_runbook_metadata():
    chunks = load_runbook_chunks(DATA_DIR / "runbooks")
    trn = [c for c in chunks if c.doc_id == "RB-TRN-001"]
    assert trn and "reroute_backhaul_path" in trn[0].metadata["actions"]
    assert "LINK_DOWN" in trn[0].metadata["alarm_codes"]


def test_retries_with_rewritten_query_when_first_is_weak():
    store = InMemoryVectorStore()
    store.add(load_runbook_chunks(DATA_DIR / "runbooks"))
    retriever = AgenticRetriever(store)
    vague = [{"code": "XYZ_UNKNOWN", "text": "qqq zzz"}]
    result = retriever.retrieve(vague, ["AMF"])
    assert len(result.attempts) >= 2
    assert result.hits, "the broader rewrite should still find AMF material"
