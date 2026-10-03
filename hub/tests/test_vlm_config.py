"""The hub offers its shared AI to every site in a sharing org except the site that serves it."""
from hub.agents import AgentRegistry
from hub.config import settings


def test_serving_site_gets_no_remote_vlm(monkeypatch):
    monkeypatch.setattr(settings, "vllm_url", "")
    monkeypatch.setattr(settings, "vllm_site", "s_server")
    monkeypatch.setattr(settings, "vllm_model", "qwen3.5:9b")
    monkeypatch.setattr(settings, "public_url", "https://hub.example")
    org = {"id": "o_1", "ai_shared": True}
    other = AgentRegistry.vlm_config(org, "tok", "s_other")
    assert other and other["url"] == "https://hub.example/v1" and other["key"] == "tok"
    assert AgentRegistry.vlm_config(org, "tok", "s_server") is None
    assert AgentRegistry.vlm_config({"id": "o_1", "ai_shared": False}, "tok", "s_other") is None
