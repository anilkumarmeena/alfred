"""Kept so `python -m alfred.voice.agent` (the voice service, make console and make dev) still starts the voice
process, which is alfred.apps.agent."""

if __name__ == "__main__":
    from alfred.apps import agent

    agent.main()
