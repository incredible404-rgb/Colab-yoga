# Notes for coding agents

- Colab-yoga is the general tool. Read "What belongs here" in [CONTRIBUTING.md](CONTRIBUTING.md) before adding anything: the console and server know no particular model, prompt or folder. One person's models, working folders, presets and one-machine migration code stay out of the code; the runners file and the settings carry them.
- colab-gpu-llm-service is a separate project. Everything the two rely on in each other is listed in [docs/integration.md](docs/integration.md): the runners file, the colab-cli release (the same pin in both), the shared Google token and sessions, the local MCP helper, the keep-alive state and the bridge commands. Change a row in both repositories or not at all; rely on nothing outside that list.
- Check before every commit: `python tools/install.py --root <a temp dir> --check`. It must pass on Windows and macOS; tests stay offline.
- Changes reach `main` through a pull request with green CI. A release is a tag `vX.Y.Z` matching `console/mygo.json`; the Release workflow builds both zips into a draft.
