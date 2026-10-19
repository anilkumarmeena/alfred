"""Old command path of alfred.apps.audio_file, which audio-file tasks made before the move still rerun."""

if __name__ == "__main__":
    import runpy

    runpy.run_module("alfred.apps.audio_file", run_name="__main__")
