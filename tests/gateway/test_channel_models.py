"""Tests for per-channel/per-topic model bindings."""

from types import SimpleNamespace

from gateway.config import Platform
from gateway.platforms.base import resolve_channel_model, resolve_channel_voice
from gateway.run import GatewayRunner
from gateway.session import SessionSource


def test_resolve_channel_model_exact_topic_mapping():
    extra = {
        "channel_models": {
            "9094": {"provider": "ollama-launch", "model": "glm-5.2:cloud"},
        }
    }

    assert resolve_channel_model(extra, "9094") == {
        "provider": "ollama-launch",
        "model": "glm-5.2:cloud",
    }


def test_resolve_channel_model_preserves_runtime_options():
    extra = {
        "channel_models": {
            "9094": {
                "provider": "ollama-launch",
                "model": "glm-5.2:cloud",
                "max_tokens": 12000,
                "context_length": 1000000,
                "compression": {
                    "enabled": True,
                    "threshold": 0.08,
                    "protect_last_n": 16,
                },
            },
        }
    }

    assert resolve_channel_model(extra, "9094") == {
        "provider": "ollama-launch",
        "model": "glm-5.2:cloud",
        "max_tokens": 12000,
        "context_length": 1000000,
        "compression": {
            "enabled": True,
            "threshold": 0.08,
            "protect_last_n": 16,
        },
    }


def test_resolve_channel_model_parent_fallback_and_string_value():
    extra = {"channel_models": {"-100123": "gpt-5.5"}}

    assert resolve_channel_model(extra, "9094", parent_id="-100123") == {
        "model": "gpt-5.5",
    }


def test_resolve_channel_voice_mapping_and_string():
    extra = {
        "channel_voices": {
            "9094": {"provider": "elevenlabs", "voice_id": "ABC123", "auto_tts": True},
            "-100123": "VOICE-ID-2",
        }
    }

    assert resolve_channel_voice(extra, "9094") == {
        "provider": "elevenlabs",
        "voice_id": "ABC123",
        "auto_tts": True,
    }
    assert resolve_channel_voice(extra, "9094", parent_id="-100123") == {
        "provider": "elevenlabs",
        "voice_id": "ABC123",
        "auto_tts": True,
    }
    assert resolve_channel_voice(extra, "9999", parent_id="-100123") == {
        "voice_id": "VOICE-ID-2",
    }


def test_gateway_runner_resolves_topic_binding_from_adapter_extra():
    runner = object.__new__(GatewayRunner)
    runner.__dict__["adapters"] = {
        Platform.TELEGRAM: SimpleNamespace(
            config=SimpleNamespace(
                extra={
                    "channel_models": {
                        "9094": {
                            "provider": "ollama-launch",
                            "model": "glm-5.2:cloud",
                        }
                    }
                }
            )
        )
    }
    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="-1003734569758",
        thread_id="9094",
    )

    assert runner._resolve_channel_model_binding(source) == {
        "provider": "ollama-launch",
        "model": "glm-5.2:cloud",
    }


def test_gateway_runner_converts_binding_to_runtime_override(monkeypatch):
    import hermes_cli.runtime_provider as runtime_provider

    def fake_resolve_runtime_provider(*, requested=None, explicit_base_url=None, explicit_api_key=None):
        assert requested == "ollama-launch"
        return {
            "provider": "ollama-launch",
            "api_key": "test-key",
            "base_url": "https://ollama.test/v1",
            "api_mode": "openai",
        }

    monkeypatch.setattr(runtime_provider, "resolve_runtime_provider", fake_resolve_runtime_provider)

    runner = object.__new__(GatewayRunner)
    override = runner._runtime_from_channel_model_binding(
        {
            "provider": "ollama-launch",
            "model": "glm-5.2:cloud",
            "context_length": 1000000,
            "max_tokens": 12000,
            "compression": {"threshold": 0.08},
        }
    )

    assert override == {
        "model": "glm-5.2:cloud",
        "provider": "ollama-launch",
        "api_key": "test-key",
        "base_url": "https://ollama.test/v1",
        "api_mode": "openai",
        "args": [],
        "context_length": 1000000,
        "max_tokens": 12000,
        "compression_config": {"threshold": 0.08},
    }
