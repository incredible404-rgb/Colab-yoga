# Notes for coding agents

- Colab-yoga is the general tool. Read "What belongs here" in [CONTRIBUTING.md](CONTRIBUTING.md) before adding anything: the console and server know no particular model, prompt or folder. One person's models, working folders, presets and one-machine migration code stay out of the code; the runners file and the settings carry them.
- colab-gpu-llm-service is a separate project. The only contract between the two is the runners file it exports (`slug`, `alias`, `title`, `name`, `tags`, `note`, `root_dir`, `files`, `code`); change it in both repositories or not at all.
- Check before every commit: `python tools/install.py --root <a temp dir> --check`. It must pass on Windows and macOS; tests stay offline.
- Changes reach `main` through a pull request with green CI. A release is a tag `vX.Y.Z` matching `console/mygo.json`; the Release workflow builds both zips into a draft.
