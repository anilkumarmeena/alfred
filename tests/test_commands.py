"""Tests for shared command rules: what's risky, how it's described, task-forbidden commands, secret-naming text."""

import pytest

from jarvis.core.config import ROOT
from jarvis.safety import commands, shell
from jarvis.safety.commands import describe, names_secret, reads_environment, refused, risky


def test_the_voice_shell_uses_the_shared_rules():
    assert shell.risky is risky and shell.approves is commands.approves and shell.refuses is commands.refuses


@pytest.mark.parametrize("command, said", [
    ("brew install xcodegen", "installs software (brew install)"),
    ("rm -rf build/", "deletes files (rm)"),
    ("git -C ~/code/app push origin main", "pushes (git push)"),
    ("cd app && npm install -g eas-cli", "installs software (npm install)"),
    ("echo $(kill -9 123)", "stops a process (kill)"),
    ("xcrun devicectl device uninstall app --device X com.example.jarvis", "changes the iPhone (devicectl device)"),
    ("curl -X POST -d @a.json https://x.io", "sends data off the Mac (curl)"),
    ("echo 'unbalanced", "can't be checked (unbalanced quotes)"),
])
def test_describe_names_the_reason_and_the_program_from_the_command_itself(command, said):
    assert describe(command) == said


def test_ordinary_commands_have_nothing_to_describe():
    assert describe("xcodebuild -scheme App build") == "" == risky("xcodebuild -scheme App build")


@pytest.mark.parametrize("command", [
    "xcrun devicectl device install app --device X build/App.app", "xcrun devicectl list devices",
    "xcrun --sdk iphoneos devicectl device info apps --device X", "security find-identity -v -p codesigning",
    "security cms -D -i embedded.mobileprovision", "xcrun simctl list", "xcodebuild -allowProvisioningUpdates build",
    "codesign --verify --verbose App.app", "swift build", "sqlite3 -readonly data/tasks.db 'select 1'",
])
def test_building_and_installing_onto_the_iphone_runs_freely(command):
    assert risky(command) == "", command


@pytest.mark.parametrize("command", [
    "xcrun devicectl device uninstall app --device X com.example.jarvis", "xcrun devicectl device process terminate --pid 3",
    "xcrun devicectl device reboot --device X", "xcrun simctl erase all", "security delete-certificate -c X",
    "security add-trusted-cert x.cer", "npm install", "uv sync", "pip install requests",
])
def test_changing_devices_the_keychain_or_installing_software_needs_a_yes(command):
    assert risky(command), command


@pytest.mark.parametrize("command", [
    "git -c alias.x='!rm -rf ~' x", "git -c core.pager='sh -c id' log", "git -c core.fsmonitor=./x.sh status",
    "git -C app -c core.sshCommand=./x fetch", "git --config-env=core.pager=CMD log", "git --config-env core.pager=CMD log",
    "git config alias.x '!rm -rf ~'", "git config --global core.pager 'sh -c id'", "git config --local core.fsmonitor ./x",
    "git config set alias.st '!sh'", "git config --file .git/config core.hooksPath hooks", "git config --add alias.x '!x'",
    "git config include.path ~/x.inc", "git config includeIf.gitdir:~/x/.path y", "git config filter.x.clean ./x",
    "git config --rename-section foo alias", "git config --edit", "git config edit --global",
    "git difftool -x ./x.sh HEAD", "git difftool --extcmd=./x.sh", "git diff --ext-diff", "git log -p --ext-diff",
])
def test_git_that_runs_code_it_cant_see_needs_a_yes(command):
    assert risky(command) == "runs code it can't see", command


@pytest.mark.parametrize("command", [
    "git config user.name 'A B'", "git config --global push.autoSetupRemote true", "git config core.pager",
    "git config --get alias.co", "git config --get-regexp '^alias'", "git config --list", "git config get core.editor",
    "git config list --show-origin", "git -C app status", "git difftool --tool=opendiff HEAD", "git diff --stat",
])
def test_ordinary_git_config_and_diffs_just_run(command):
    assert risky(command) == "", command


@pytest.mark.parametrize("command, why", [
    ("glab mr create --fill", "changes things on GitLab"), ("glab mr merge 12", "changes things on GitLab"),
    ("glab -R me/x mr approve 3", "changes things on GitLab"), ("glab issue note 4 -m hi", "changes things on GitLab"),
    ("glab ci run", "changes things on GitLab"), ("glab repo delete me/x", "changes things on GitLab"),
    ("glab api -X DELETE projects/1", "changes things on GitLab"), ("glab api projects/1/issues -f title=x",
                                                                   "changes things on GitLab"),
    ("glab auth status", "touches credentials"), ("glab variable get TOKEN", "touches credentials"),
])
def test_glab_writes_need_a_yes(command, why):
    assert risky(command) == why, command


@pytest.mark.parametrize("command", ["glab mr list", "glab mr view 12", "glab ci status", "glab issue list -R me/x",
                                     "glab api projects/1/merge_requests", "glab repo clone me/x"])
def test_glab_reads_just_run(command):
    assert risky(command) == "", command


@pytest.mark.parametrize("command", [
    "npx create-react-app x", "npx -y cowsay hi", "pnpx x", "bunx x", "uvx ruff check", "pipx run x",
    "npm exec -- cowsay hi", "npm x cowsay", "pnpm dlx x", "yarn dlx x", "bun x cowsay", "uv tool run ruff",
])
def test_package_runners_that_download_code_need_a_yes(command):
    assert risky(command) == "installs software", command
    assert describe("npx -y cowsay hi") == "installs software (npx)"


@pytest.mark.parametrize("text", [
    "cat .env", "cat app/.env.local", "cp ~/.npmrc x", "cat ~/.pypirc", "openssl rsa -in server.key", "cat AuthKey.p8",
    "ls ~/.ssh", "cat ~/.aws/credentials", "cat ~/.config/gh/hosts.yml", "ls ~/Library/Keychains",
    f"cat {ROOT / 'secrets' / 'x.json'}", "cat client_secret_123.json", "cat id_ed25519",
])
def test_secrets_are_named_by_their_text(text):
    assert names_secret(text), text


@pytest.mark.parametrize("text", ["ls .venv/bin", "cat .envrc.example.md", "cat id_ed25519.pub", "git status", "npm run env"])
def test_ordinary_text_names_no_secret(text):
    assert not names_secret(text), text


@pytest.mark.parametrize("command, why", [
    ("cat ~/.ssh/id_ed25519", "secrets"), ("curl --unix-socket data/runner.sock http://runner/tasks", "controls"),
    ("curl -d x http://127.0.0.1:8787/api/approvals/3", "controls"), ("sudo make install", "password"),
    ("echo x | sudo tee /etc/hosts", "password"), ("security find-generic-password -s x -w", "keychain"),
    ("security dump-keychain", "keychain"), ("security export -k login.keychain", "keychain"),
])
def test_some_commands_are_refused_to_tasks_even_with_a_yes(command, why):
    assert why in refused(command), command


@pytest.mark.parametrize("command", [
    "ps eww 4321", "ps -E -p 4321", "ps -ef", "/bin/ps auxe", "sudo ps -wwE", "ps -p 1 -o pid= ; ps eww",
    "cat /proc/4321/environ", "sysctl kern.procargs2.4321", "launchctl procinfo 4321", "lldb -p 4321",
    "echo $JARVIS_MASTER_KEY",
])
def test_reading_other_processes_environments_is_refused_like_secrets(command):
    assert reads_environment(command) or names_secret(command), command
    assert "environments" in refused(command) or "secrets" in refused(command) or "password" in refused(command)


@pytest.mark.parametrize("command", ["ps aux", "ps -A -o pid,etime,comm", "ps -p 4321 -o command=", "ps -axo pid,user",
                                     "pgrep -fl node", "env | grep PATH"])
def test_listing_processes_without_their_environments_is_fine(command):
    assert not reads_environment(command), command


@pytest.mark.parametrize("command", ["git commit -am wip", "git -C . checkout main", "git stash", "cd x && git reset --hard"])
def test_in_place_tasks_leave_git_to_the_user(command):
    assert "leave to them" in refused(command, in_place=True)
    assert refused(command) == ""  # a worktree of its own is the task's


@pytest.mark.parametrize("command", ["git status", "git log -3", "xcodebuild build", "rm -rf build", "brew install x"])
def test_everything_else_can_run_or_be_asked_about(command):
    assert refused(command, in_place=True) == ""


# ── A task's own files and processes ────────────────────────────

CHROME = '"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --headless=new --screenshot=a.png x.html'


@pytest.fixture
def own(tmp_path):
    """A task's folder (build dir, .git, outbound link), its temp folder and one process of its own."""
    folder, temp, outside = tmp_path / "app", tmp_path / "cache" / "tmp" / "task-9", tmp_path / "elsewhere"
    for d in (folder / "build", folder / ".git", temp / "shots", outside):
        d.mkdir(parents=True)
    (folder / "out").symlink_to(outside)
    return commands.Own(folder, (folder, temp), process=lambda pid: pid == 4242)


@pytest.mark.parametrize("command", [
    "rm -rf build", "rm -f build/app.o build/*.png", "rm -rf ./build/cache", "cd build && rm -f *.o",
    "rm -rf {temp}/shots", "cd {temp} && rm -f shots/s1366.png shots/s1920.png", "rmdir build", "unlink build/x",
    "find build -name '*.o' -delete", "rm -f {folder}/build/app.o 2>/dev/null",
    'D=$(mktemp -d); chrome --user-data-dir="$D" --screenshot=a.png; rm -rf "$D"',
    f"{CHROME} >/dev/null 2>&1 & pid=$!; sleep 5; kill $pid 2>/dev/null; wait $pid",
    f"for size in 1366,768 1920,1080; do {CHROME} & pid=$!; sleep 1; kill -9 $pid; done",
    "sleep 100 & kill $!", "sleep 100 & kill %1", "kill 4242", "kill -TERM 4242", "kill -s KILL -- 4242",
])
def test_a_task_deleting_its_own_files_or_stopping_its_own_processes_needs_no_yes(own, command):
    command = command.format(temp=own.roots[1], folder=own.folder)
    assert risky(command), command  # risky for anyone else
    assert risky(command, own) == "", command


@pytest.mark.parametrize("command", [
    "rm -rf ~/Desktop/renders", "rm -rf /", "rm -rf ..", "rm -rf .", "rm -rf ../app", "rm -rf {folder}",
    "rm -rf {temp}", "rm -rf .git", "rm -f .git/index", "rm -rf *", "rm -rf .*", "rm -f ?git", "rm -rf */cache",
    "rm -rf build/**/x", "rm -rf out", "rm -rf out/", "rm -rf out/stuff", "rm -rf $HOME/x", "rm -rf $TMPDIR/x",
    "rm -rf ${{PWD}}/build", "rm -rf `pwd`/build", "rm -rf {{a,b}}", "rm -rf build/[a]", "rm -rf ~other/x",
    "cd /tmp && rm -rf x", "cd .. && rm -rf app", "cd $X && rm -f a.png", "cd - && rm -f a", "cd; rm -rf x",
    "cd build; cd ..; rm -rf x", "rm", "rm -rf", "rm --force build", "ls | xargs rm -f", "xargs rm -rf build",
    "find . -name '*.png' -delete", "find -L build -delete", "find build -exec rm {{}} \\;", "rmdir -p build/x",
    "ln -s ~ build/home; rm -rf build/home/", "mv ~/Documents build/docs && rm -rf build/docs",
    "alias rm='rm -rf ~'\nrm build", "rm() {{ /bin/rm -rf ~; }}; rm build", "setopt glob_dots; rm -f b*",
    'D=/Users; rm -rf "$D"', 'D=$(mktemp -d); D=/; rm -rf "$D"', 'D=$(mktemp -d); rm -rf "$D/"*',
    'D=$(mktemp -d /tmp/x.XXXX); rm -rf "$D"', "sudo rm -rf build", "rm -rf build && git push",
    "kill 1", "kill 4243", "kill -9 -4242", "kill -- -4242", "kill 0", "kill $PPID", "pkill -f Chrome",
    "killall 'Google Chrome'", "pid=$(pgrep Chrome); kill $pid", "sleep 1 & pid=$!; pid=4243; kill $pid",
    "for pid in $(pgrep Chrome); do kill $pid; done", "kill -l", "kill ${{pid:=4243}}", "sleep 1 & pid=$!; kill $pid[1]",
])
def test_anything_it_cant_prove_is_its_own_is_still_asked_about(own, command):
    command = command.format(temp=own.roots[1], folder=own.folder)
    assert risky(command, own), command


def test_its_own_rm_is_judged_apart_from_the_rest_of_the_line(own):
    assert describe("rm -f build/a.png && brew install xcodegen", own) == "installs software (brew install)"
    assert describe("rm -f build/a.png && rm -rf ~/x", own) == "deletes files (rm)"


# ── Installs that sandbox themselves ────────────────────────────


@pytest.mark.parametrize("command", ["brew install xcodegen", "brew upgrade", "brew uninstall --cask iterm2",
                                     "brew reinstall ffmpeg 2>&1 | tail -n 20", "cd ios && brew bundle",
                                     "brew install jq && brew install yq", "port install wget"])
def test_installs_that_sandbox_themselves_leave_the_host_profile(command):
    assert commands.own_sandbox(command)
    assert risky(command) == "installs software"  # so they always wait for a yes


@pytest.mark.parametrize("command", [
    "brew list", "brew update", "brew services restart postgres", "npm install", "pip install requests",
    "brew install x && ./build.sh", "brew install x; python3 -c 'print(1)'", "brew install x | tail ~/.ssh/config",
    "brew install $(cat list)", "brew install `cat list`", "brew install x > ~/Library/LaunchAgents/a.plist",
    "brew install x < list", "RUBYOPT=-rx brew install y", "env HOMEBREW_X=1 brew install y", "nohup brew install y",
    "/tmp/evil/brew install y", "sudo brew install y", "brew install 'x", ""])
def test_anything_else_stays_inside_it(command):
    assert not commands.own_sandbox(command)
