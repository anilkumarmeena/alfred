"""Tests for shared command rules: what's risky, how it's described, task-forbidden commands, secret-naming text."""

import fnmatch
import re

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
    ("curl -X POST -d @a.json https://x.io", "sends data off the computer (curl)"),
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
    "ps eww 4321", "ps -E -p 4321", "/bin/ps auxe", "sudo ps -wwE", "ps -p 1 -o pid= ; ps eww",
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


# ── Linux ───────────────────────────────────────────────────────


@pytest.mark.parametrize("command, why", [
    # services, sessions and power: what launchctl and shutdown say on macOS
    ("systemctl --user restart jarvis-agent", "changes the system (systemctl restart)"),
    ("systemctl stop nginx", "changes the system (systemctl stop)"),
    ("systemctl start nginx.service", "changes the system (systemctl start)"),
    ("systemctl enable --now foo.timer", "changes the system (systemctl enable)"),
    ("systemctl --user disable foo", "changes the system (systemctl disable)"),
    ("systemctl mask bluetooth", "changes the system (systemctl mask)"),
    ("systemctl --user daemon-reload", "changes the system (systemctl daemon-reload)"),
    ("systemctl -H box kill sshd", "changes the system (systemctl kill)"),
    ("systemctl edit nginx", "changes the system (systemctl edit)"),
    ("systemctl poweroff", "stops the computer (systemctl poweroff)"),
    ("systemctl suspend", "stops the computer (systemctl suspend)"),
    ("poweroff", "stops the computer (poweroff)"),
    ("systemd-run --user --unit x sleep 9", "changes the system (systemd-run)"),
    ("loginctl terminate-user bob", "changes the system (loginctl terminate-user)"),
    ("loginctl enable-linger", "changes the system (loginctl enable-linger)"),
    ("service nginx restart", "changes the system (service nginx)"),
    ("journalctl --vacuum-time=2d", "deletes files (journalctl)"), ("journalctl --vacuum-size 1G", "deletes files (journalctl)"),
    ("journalctl --rotate", "changes the system (journalctl)"), ("journalctl --flush", "changes the system (journalctl)"),
    # packages: what brew install says
    ("apt install ffmpeg", "installs software (apt install)"), ("apt-get -y remove x", "installs software (apt-get remove)"),
    ("apt -o Debug=1 upgrade", "installs software (apt upgrade)"),
    ("dnf install ffmpeg", "installs software (dnf install)"), ("dnf --repo list install x", "installs software (dnf install)"),
    ("yum remove x", "installs software (yum remove)"), ("dnf history undo 3", "installs software (dnf history)"),
    ("zypper in vim", "installs software (zypper in)"), ("zypper --non-interactive dup", "installs software (zypper dup)"),
    ("apk add curl", "installs software (apk add)"), ("apk del curl", "installs software (apk del)"),
    ("pacman -S ffmpeg", "installs software (pacman)"), ("pacman -Syu", "installs software (pacman)"),
    ("pacman -Rns x", "installs software (pacman)"), ("pacman -U x.pkg.tar.zst", "installs software (pacman)"),
    ("pacman --sync --refresh", "installs software (pacman)"), ("yay", "installs software (yay)"),
    ("paru -S x", "installs software (paru)"),
    ("snap install code --classic", "installs software (snap install)"), ("snap refresh", "installs software (snap refresh)"),
    ("snap remove x", "installs software (snap remove)"), ("snap stop x", "stops a process (snap stop)"),
    ("snap connect x:camera", "changes permissions (snap connect)"),
    ("flatpak install flathub org.x.Y", "installs software (flatpak install)"),
    ("flatpak uninstall org.x.Y", "installs software (flatpak uninstall)"),
    ("flatpak update", "installs software (flatpak update)"), ("flatpak kill org.x.Y", "stops a process (flatpak kill)"),
    ("flatpak override --filesystem=home org.x.Y", "changes permissions (flatpak override)"),
    ("dpkg -i x.deb", "installs software (dpkg)"), ("dpkg --purge x", "installs software (dpkg)"),
    ("rpm -ivh x.rpm", "installs software (rpm)"), ("rpm -e x", "installs software (rpm)"),
    ("add-apt-repository ppa:x/y", "installs software (add-apt-repository)"),
    # network and firewall
    ("nmcli connection up home", "changes the system (nmcli connection)"),
    ("nmcli c delete home", "changes the system (nmcli)"), ("nmcli radio wifi off", "changes the system (nmcli radio)"),
    ("nmcli networking off", "changes the system (nmcli networking)"),
    ("nmcli device wifi connect x password y", "changes the system (nmcli device)"),
    ("nmcli general hostname box", "changes the system (nmcli general)"),
    ("nmcli d s", "changes the system (nmcli)"),  # s could be set: asked
    ("ip link set eth0 down", "changes the system (ip link)"), ("ip addr add 10.0.0.2/24 dev eth0", "changes the system (ip addr)"),
    ("ip route del default", "changes the system (ip route)"), ("ip -4 route flush cache", "changes the system (ip route)"),
    ("ip netns exec x bash", "changes the system (ip netns)"), ("ip -batch cmds.txt", "changes the system (ip)"),
    ("iptables -A INPUT -j DROP", "changes the system (iptables)"), ("iptables -F", "changes the system (iptables)"),
    ("iptables -L -Z", "changes the system (iptables)"), ("ip6tables -P INPUT DROP", "changes the system (ip6tables)"),
    ("iptables-restore < rules", "changes the system (iptables-restore)"),
    ("nft add rule inet filter input drop", "changes the system (nft add)"), ("nft -f rules.nft", "changes the system (nft)"),
    ("nft flush ruleset", "changes the system (nft flush)"),
    ("ufw allow 22", "changes the system (ufw allow)"), ("ufw disable", "changes the system (ufw disable)"),
    ("firewall-cmd --zone=public --add-port=80/tcp", "changes the system (firewall-cmd)"),
    ("firewall-cmd --reload", "changes the system (firewall-cmd)"),
    # desktop settings, the system's settings, users, disks
    ("gsettings set org.gnome.desktop.interface clock-format 12h", "changes the system (gsettings set)"),
    ("gsettings reset org.gnome.x y", "changes the system (gsettings reset)"),
    ("dconf write /org/x 1", "changes the system (dconf write)"), ("dconf load / < x", "changes the system (dconf load)"),
    ("xdg-settings set default-web-browser x.desktop", "changes the system (xdg-settings set)"),
    ("xdg-mime default x.desktop text/html", "changes the system (xdg-mime default)"),
    ("update-alternatives --set editor /usr/bin/vim", "changes the system (update-alternatives)"),
    ("update-alternatives --config java", "changes the system (update-alternatives)"),
    ("timedatectl set-timezone Europe/London", "changes the system (timedatectl set-timezone)"),
    ("timedatectl set-ntp false", "changes the system (timedatectl set-ntp)"),
    ("hostnamectl set-hostname box", "changes the system (hostnamectl set-hostname)"),
    ("hostnamectl hostname box", "changes the system (hostnamectl hostname)"),
    ("localectl set-locale LANG=C", "changes the system (localectl set-locale)"),
    ("hostname box", "changes the system (hostname box)"),
    ("sysctl -w net.ipv4.ip_forward=1", "changes the system (sysctl)"), ("sysctl kern.x=1", "changes the system (sysctl)"),
    ("sysctl --system", "changes the system (sysctl)"), ("sysctl -p", "changes the system (sysctl)"),
    ("modprobe vfio", "changes the system (modprobe)"), ("rmmod x", "changes the system (rmmod)"),
    ("crontab -e", "changes the system (crontab)"), ("crontab -r", "changes the system (crontab)"),
    ("chsh -s /bin/zsh", "changes the system (chsh)"), ("usermod -aG docker me", "changes the system (usermod)"),
    ("passwd", "changes the system (passwd)"), ("useradd bob", "changes the system (useradd)"),
    ("mount /dev/sdb1 /mnt", "changes the system (mount)"), ("mount -a", "changes the system (mount)"),
    ("umount /mnt", "changes the system (umount)"), ("swapoff -a", "changes the system (swapoff)"),
    ("wipefs -a /dev/sdb", "writes a disk (wipefs)"), ("mkfs.ext4 /dev/sdb1", "writes a disk (mkfs.ext4)"),
    ("parted /dev/sdb mklabel gpt", "writes a disk (parted)"),
    ("chattr +i f", "changes permissions (chattr)"), ("setfacl -m u:x:r f", "changes permissions (setfacl)"),
    ("pkexec ls", "runs as root (pkexec)"), ("run0 ls", "runs as root (run0)"),
    ("dbus-send --session --dest=org.x / x.y", "controls apps (dbus-send)"),
    ("busctl call org.x / x y", "controls apps (busctl call)"), ("gdbus call --session -d x -o / -m y", "controls apps (gdbus call)"),
    ("xdotool type hi", "controls apps (xdotool)"),
    # keychains: asked here, refused to tasks
    ("secret-tool lookup service github", "reads the keychain (secret-tool)"),
    ("secret-tool search --all service x", "reads the keychain (secret-tool)"),
    ("kwallet-query -r github kdewallet", "reads the keychain (kwallet-query)"), ("pass show email", "reads the keychain (pass)"),
    ("nmcli -s connection show home", "reads the keychain (nmcli connection)"),
    ("nmcli --show-secrets c s home", "reads the keychain (nmcli)"),
    ("nmcli device wifi show-password", "reads the keychain (nmcli device)"),
])
def test_linux_commands_that_change_things_need_a_yes(command, why):
    assert describe(command) == why, command


@pytest.mark.parametrize("command", [
    "systemctl", "systemctl status nginx", "systemctl --user status jarvis-agent", "systemctl list-units --failed",
    "systemctl --user list-units --type=service", "systemctl -t service list-units", "systemctl list-timers --all",
    "systemctl is-active nginx", "systemctl is-enabled nginx", "systemctl show -p ActiveState nginx",
    "systemctl cat nginx", "systemctl list-unit-files", "loginctl", "loginctl list-sessions", "loginctl show-user me",
    "service nginx status", "service --status-all", "journalctl -u nginx -n 50", "journalctl --user -f",
    "journalctl -b -p err", "journalctl --disk-usage",
    "apt list --installed", "apt search ffmpeg", "apt show ffmpeg", "apt policy ffmpeg", "apt-get check",
    "dnf list installed", "dnf search ffmpeg", "dnf info x", "dnf check-update", "yum repolist", "zypper se vim",
    "zypper --non-interactive lr", "apk info", "apk search curl", "pacman -Ss ffmpeg", "pacman -Si x", "pacman -Qi bash",
    "pacman -Q", "pacman -Ql x", "pacman -F /usr/bin/x", "yay -Ss x", "snap list", "snap info code", "snap find x",
    "snap services", "flatpak list", "flatpak info org.x.Y", "flatpak run org.x.Y", "flatpak remotes", "dpkg -l",
    "dpkg -L x", "dpkg -S /usr/bin/x", "rpm -qa", "rpm -qi bash", "rpm -V x",
    "nmcli", "nmcli device status", "nmcli connection show", "nmcli c show", "nmcli con show home", "nmcli -t -f NAME c s",
    "nmcli device wifi list", "nmcli d wifi", "nmcli general status", "nmcli general hostname", "nmcli radio",
    "nmcli networking connectivity check", "nmcli monitor",
    "ip addr", "ip a", "ip -br a", "ip -4 addr show dev eth0", "ip link", "ip route", "ip route get 1.1.1.1",
    "ip -s link show eth0", "ip neigh", "ip netns list", "ip monitor all", "ip -n ns1 addr",
    "iptables -L -n", "iptables -nvL", "iptables -t nat -L --line-numbers", "iptables -S", "iptables-save",
    "nft list ruleset", "nft -a list table inet filter", "ufw status", "ufw status verbose",
    "firewall-cmd --state", "firewall-cmd --list-all", "firewall-cmd --zone=public --list-ports", "firewall-cmd --get-zones",
    "gsettings get org.gnome.desktop.interface clock-format", "gsettings list-keys org.gnome.x", "dconf read /org/x",
    "dconf dump /org/gnome/", "xdg-settings get default-web-browser", "xdg-mime query default text/html",
    "update-alternatives --display editor", "update-alternatives --list java", "timedatectl", "timedatectl status",
    "hostnamectl", "hostnamectl status", "hostnamectl hostname", "localectl", "localectl list-locales",
    "hostname", "hostname -f", "sysctl net.ipv4.ip_forward", "sysctl -a", "sysctl -n hw.ncpu", "mount", "mount -l",
    "busctl", "busctl --user list", "busctl introspect org.x /", "gdbus introspect --session -d x -o /",
    "lsblk", "findmnt", "ss -tlnp", "free -h", "uname -a", "lsmod", "xdg-open out.pdf", "notify-send hi",
    "cat /etc/os-release", "cp /etc/hosts ./hosts", "cp ~/.config/systemd/user/x.service /tmp/",
    "ls ~/.config/autostart", "sed s/a/b/ /etc/fstab", "cd ~/.config/systemd/user && ls",
])
def test_linux_reads_just_run(command):
    assert risky(command) == "", command


@pytest.mark.parametrize("command, why", [
    ("cp x.service ~/.config/systemd/user/", "changes the system (cp)"),
    ("ln -s ~/x.service ~/.config/systemd/user/x.service", "changes the system (ln)"),
    ("install -m644 x.service $HOME/.config/systemd/user/x.service", "changes the system (install)"),
    ("cp -t ~/.local/share/systemd/user x.service", "changes the system (cp)"),
    ("cp x.desktop ~/.config/autostart/", "changes the system (cp)"),
    ("mkdir -p ~/.config/autostart", "changes the system (mkdir)"),
    ("curl -o ~/.config/autostart/x.desktop https://x.io/x", "changes the system (curl)"),
    ("tee ~/.config/environment.d/x.conf < x", "changes the system (tee)"),
    ("echo x | tee -a /etc/hosts", "changes the system (tee)"), ("sed -i s/a/b/ /etc/fstab", "changes the system (sed)"),
    ("cp x.conf /etc/nginx/conf.d/", "changes the system (cp)"), ("touch /etc/x", "changes the system (touch)"),
    ("echo 127.0.0.1 x > /etc/hosts", "changes the system (a redirect)"),
    ("cd ~/.config/systemd/user && cp ~/x.service .", "changes the system (cp)"),
    ("cd /etc && echo x > hosts", "changes the system (a redirect)"),
    ("cp a.plist ~/Library/LaunchAgents/", "changes the system (cp)"),  # macOS's own login items, too
    ("cp a.plist /Library/LaunchDaemons/", "changes the system (cp)"),
    ("cp x ~/.bashrc", "overwrites a settings file (cp)"), ("cp x ~/.profile", "overwrites a settings file (cp)"),
    ("echo 'x' >> ~/.config/systemd/user/x.service", "overwrites a settings file (a redirect)"),
])
def test_writing_what_runs_at_login_or_system_wide_needs_a_yes(command, why):
    assert describe(command) == why, command


@pytest.mark.parametrize("command", [
    "secret-tool lookup service github", "secret-tool search service x", "secret-tool store --label=x service y",
    "secret-tool clear service x", "kwallet-query -r github kdewallet", "pass show email", "pass", "pass -c email",
    "nmcli -s connection show home", "nmcli device wifi show-password",
    "busctl --user call org.freedesktop.secrets /org/freedesktop/secrets x y",
    "gdbus call --session -d org.kde.kwalletd5 -o /modules/kwalletd5 -m org.kde.KWallet.readPassword",
])
def test_linux_keychain_reads_are_refused_to_tasks_like_the_macs(command):
    assert refused(command) == "it reads or changes passwords in the keychain", command


@pytest.mark.parametrize("command", ["pkexec ls", "run0 systemctl restart x"])
def test_linux_root_tools_are_refused_like_sudo(command):
    assert "password typed in a terminal" in refused(command)


@pytest.mark.parametrize("command", ["secret-tool lock --collection=x", "nmcli c show", "systemctl status x"])
def test_linux_commands_that_read_no_password_are_not_refused(command):
    assert refused(command) == ""


@pytest.mark.parametrize("command, target", [
    ("make services-restart", "make services-restart"),
    ("cd ~/jarvis && make -C . services-stop", "make services-stop"),
    ("systemctl --user restart com.jarvis.agent", ""), ("systemctl --user -q enable foo.service", ""),
    ("systemd-run --user --scope sleep 1", ""),
])
def test_on_linux_tasks_leave_the_users_systemd_session_to_them(command, target, monkeypatch):
    monkeypatch.setattr(commands.platform, "LINUX", True)
    why = refused(command)
    assert (f"ask them to run {target}" in why) if target else why == commands.USER_BUS, command
    monkeypatch.setattr(commands.platform, "LINUX", False)
    assert refused(command) == ""  # the Mac's launchd is reachable: nothing to refuse


@pytest.mark.parametrize("command", ["systemctl --user status com.jarvis.agent", "systemctl --user", "make test",
                                     "systemctl restart nginx", "make services"])
def test_on_linux_other_systemd_and_make_commands_are_not_refused_for_the_bus(command, monkeypatch):
    monkeypatch.setattr(commands.platform, "LINUX", True)
    assert refused(command) == ""


@pytest.mark.parametrize("text", [
    "ls ~/.local/share/keyrings", "cat ~/.local/share/keyrings/login.keyring", "ls ~/.local/share/kwalletd",
    "cat ~/.password-store/email.gpg", "cp -r ~/.config/Bitwarden /tmp", "certutil -L -d ~/.pki/nssdb",
])
def test_linux_credential_stores_are_secrets(text):
    assert names_secret(text), text
    assert "secrets" in refused(text)


REAL_ENVS = [".env", ".env.local", ".env.production", ".env.staging", ".env.test", ".env.", ".env.e", ".env.exampl",
             ".env.examples", ".env.example.local", ".env.sampled", ".env.tem", ".env.Local"]
ENV_TEMPLATES = [".env.example", ".env.sample", ".env.template"]


@pytest.mark.parametrize("text", [
    "sed -i '/HEARTBEAT/d' .env.example", "cat app/.env.sample", f"cat {ROOT / '.env.example'}",
    "cp templates/.env.template ./x", 'grep -n JARVIS "config/.env.example"',
])
def test_env_templates_are_not_secrets(text):
    assert not names_secret(text), text
    assert not refused(text), text


@pytest.mark.parametrize("text", [
    *(f"cat app/{name}" for name in REAL_ENVS),
    f"cat {ROOT / '.env'}", "cp .env.example .env", "cat .env.example>.env", "diff .env.example .env.local",
    "cat .env.example-old",
])
def test_real_env_files_are_still_secrets_beside_a_template(text):
    assert names_secret(text), text


def test_secret_paths_leave_out_env_templates_only(tmp_path):
    from pathlib import Path

    for name in REAL_ENVS:
        assert commands.secret_path(tmp_path / name) and commands.SECRET_FILES.fullmatch(name), name
    for name in ENV_TEMPLATES:
        assert not commands.secret_path(tmp_path / name), name
    assert commands.secret_path(Path.home() / ".ssh" / ".env.example")  # a secrets folder hides everything in it


def _sandbox_regex(glob: str) -> re.Pattern:
    """A glob as Claude Code's sandbox turns it into a Seatbelt regex (sandbox-runtime's globToRegex)."""
    rx = re.sub(r"[.^$+{}()|\\]", lambda m: "\\" + m[0], glob)
    rx = re.sub(r"\[([^\]]*?)$", r"\\[\1", rx).replace("**/", "\0").replace("**", "\1").replace("*", "[^/]*")
    return re.compile("^" + rx.replace("?", "[^/]").replace("\0", "(.*/)?").replace("\1", ".*") + "$")


@pytest.mark.parametrize("match", [
    lambda glob, path: fnmatch.fnmatchcase(path.rsplit("/", 1)[-1], glob.removeprefix("**/")),  # as git and gitignore
    lambda glob, path: bool(_sandbox_regex("/p/" + glob).match(path)),
], ids=["glob", "sandbox"])
def test_the_deny_globs_hide_every_real_env_file_and_no_template(match):
    for name in REAL_ENVS:
        assert any(match(g, f"/p/app/{name}") for g in commands.SECRET_PATTERNS), name
    for name in ENV_TEMPLATES:
        assert not [g for g in commands.SECRET_PATTERNS if match(g, f"/p/app/{name}")], name
    assert all("^" not in g and "[!" not in g for g in commands.SECRET_PATTERNS)  # neither negates in the sandbox


def test_linux_credential_stores_are_secret_paths_wherever_home_is():
    from pathlib import Path

    for rel in (".local/share/keyrings/login.keyring", ".password-store/x.gpg", ".local/share/kwalletd/kdewallet.kwl",
                ".config/Bitwarden/data.json", ".pki/nssdb/key4.db"):
        assert commands.secret_path(Path.home() / rel), rel


@pytest.mark.parametrize("command", ["ps -ef", "ps -e -o pid,comm", "ps -elf"])
def test_ps_e_lists_every_process_on_linux_without_their_environments(monkeypatch, command):
    monkeypatch.setattr(commands.platform, "MAC", True)
    assert reads_environment(command)  # on macOS, -e shows environments
    monkeypatch.setattr(commands.platform, "MAC", False)
    monkeypatch.setattr(commands.platform, "LINUX", True)
    assert not reads_environment(command), command
    assert reads_environment("ps eww 1") and reads_environment("ps auxe") and reads_environment("cat /proc/1/environ")


@pytest.mark.parametrize("command", ["strace -p 4321", "sudo gdb -p 4321", "pkexec --user root ps eww"])
def test_attaching_to_another_process_reads_its_environment(command):
    assert reads_environment(command), command


@pytest.mark.parametrize("said, command", [
    ("Restart nginx.", "systemctl restart nginx"),
    ("Restart the nginx service.", "systemctl --user restart nginx.service"),
    ("Stop the jarvis agent.", "systemctl --user stop jarvis-agent"),
    ("Install ffmpeg.", "dnf install ffmpeg"), ("Install ffmpeg.", "apt install ffmpeg"),
    ("Install ffmpeg.", "apt-get install -y ffmpeg"),
])
def test_ordering_a_linux_service_or_install_is_its_go_ahead(said, command):
    assert commands.ordered(said, command), (said, command)


@pytest.mark.parametrize("said, command", [
    ("Restart nginx.", "systemctl restart sshd"),  # each unit by name
    ("Stop it.", "systemctl stop nginx"), ("Restart the service.", "systemctl restart nginx.service"),
    ("Restart nginx.", "systemctl enable --now nginx"), ("Restart nginx.", "systemctl daemon-reload"),
    ("Install ffmpeg.", "pacman -S ffmpeg"),  # no subcommand to check against what was said
])
def test_other_linux_service_changes_are_still_asked(said, command):
    assert not commands.ordered(said, command), (said, command)


@pytest.mark.parametrize("command, remembered", [
    ("dnf install ffmpeg", True), ("apt-get install -y x", True), ("pacman -S x", True), ("snap stop x", True),
    ("flatpak override --filesystem=home x", True),
    ("systemctl restart nginx", False), ("secret-tool lookup a b", False), ("cp x.service ~/.config/systemd/user/", False),
])
def test_linux_kinds_reuse_the_remembered_kinds(command, remembered):
    from jarvis.safety import remembered as rules

    offered = rules.pieces(command)
    assert (not offered["never"]) is remembered, offered
