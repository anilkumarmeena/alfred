"""Kept so `python -m jarvis.voice.agent` (the voice service, make console and make dev) still starts the voice
process, which is jarvis.apps.agent."""

if __name__ == "__main__":
    from jarvis.apps import agent

    agent.main()
