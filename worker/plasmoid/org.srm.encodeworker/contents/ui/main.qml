import QtQuick
import QtQuick.Layouts
import org.kde.plasma.plasmoid
import org.kde.plasma.components as PlasmaComponents3
import org.kde.plasma.plasma5support as P5Support
import org.kde.kirigami as Kirigami

// Shows the state of worker/srm_encode_worker.py (it writes $XDG_RUNTIME_DIR/srm-encode-worker/status.json) and starts / stops / restarts
// the systemd user service srm-encode-worker (worker/install.sh sets it up).
PlasmoidItem {
    id: root

    readonly property string service: "srm-encode-worker"
    property string serviceState: "unknown"   // systemctl --user is-active
    property var status: null                  // the worker's JSON, null = no file
    property double now: Date.now() / 1000
    property bool busy: false                  // a start/stop/restart command is running

    // the worker rewrites its file at least every `interval` seconds while it lives
    readonly property bool alive: status !== null && status.state !== "stopped"
        && (now - status.updated_at) < Math.max(45, (status.interval || 30) * 2 + 15)
    readonly property bool encoding: alive && status.state === "encoding"
    readonly property bool byHand: alive && (serviceState === "inactive" || serviceState === "failed")   // running, but not as our service: the widget cannot stop it
    readonly property bool autoQueue: alive && status.auto_queue === true   // the worker's own switch file, see srm_encode_worker.auto_queue_file()
    // a finished job is shown for 20 s only; a failure stays until the next job
    readonly property bool showLast: status !== null && status.last_result !== null && status.last_result !== undefined
        && (!status.last_result.ok || (now - status.last_result.at) < 20)
    readonly property real percent: encoding && status.percent !== null ? Math.max(0, Math.min(100, status.percent)) : 0

    function fmtLeft(s) {
        if (s === null || s === undefined) return ""
        s = Math.round(s)
        if (s < 90) return s + " s"
        if (s < 5400) return Math.round(s / 60) + " min"
        return Math.floor(s / 3600) + " h " + Math.round((s % 3600) / 60) + " min"
    }
    function fmtAgo(t) {
        const s = Math.max(0, Math.round(now - t))
        if (s < 90) return "just now"
        if (s < 5400) return Math.round(s / 60) + " min ago"
        return Math.round(s / 3600) + " h ago"
    }
    readonly property string headline: {
        if (!alive) return serviceState === "failed" ? "Failed" : "Stopped"
        if (status.state === "encoding") return "Encoding " + Math.round(percent) + " %"
        if (status.state === "unreachable") return "Sidecar not reachable"
        return "Waiting for work"
    }

    Plasmoid.icon: alive ? (encoding ? "media-playback-start" : (status.state === "unreachable" ? "dialog-warning" : "media-playback-pause")) : "media-playback-stop"
    toolTipMainText: "Encode worker: " + headline
    toolTipSubText: encoding ? status.file + (status.seconds_left !== null && status.seconds_left !== undefined ? "\n" + fmtLeft(status.seconds_left) + " left" : "") : ""

    P5Support.DataSource {
        id: exec
        engine: "executable"
        connectedSources: []
        onNewData: (source, data) => {
            disconnectSource(source)
            if (source.indexOf("is-active") >= 0) {
                const out = (data["stdout"] || "")
                const nl = out.indexOf("\n")
                root.serviceState = (nl < 0 ? out : out.substring(0, nl)).trim()
                const json = nl < 0 ? "" : out.substring(nl + 1).trim()
                try { root.status = json ? JSON.parse(json) : null } catch (e) { root.status = null }
                root.now = Date.now() / 1000
            } else {
                root.busy = false
                poll()
            }
        }
    }
    function poll() {
        exec.connectSource("systemctl --user is-active " + service + "; cat \"${XDG_RUNTIME_DIR:-/tmp/srm-$(id -u)}/srm-encode-worker/status.json\" 2>/dev/null")
    }
    function setAutoQueue(on) {
        const f = "\"${XDG_CONFIG_HOME:-$HOME/.config}/srm-encode-worker/auto-queue\""
        busy = true
        exec.connectSource(on ? "mkdir -p \"$(dirname " + f + ")\" && touch " + f + "; echo done-auto" : "rm -f " + f + "; echo done-auto")
    }
    function control(verb) {
        busy = true
        exec.connectSource("systemctl --user " + verb + " " + service + " 2>&1; echo done-" + verb)
    }
    Timer { interval: 2000; running: true; repeat: true; triggeredOnStart: true; onTriggered: root.poll() }

    compactRepresentation: MouseArea {
        onClicked: root.expanded = !root.expanded
        Kirigami.Icon { anchors.fill: parent; source: Plasmoid.icon; opacity: root.alive ? 1 : 0.5 }
        // a thin progress line along the bottom edge while encoding
        Rectangle {
            visible: root.encoding
            anchors { left: parent.left; bottom: parent.bottom }
            height: Math.max(3, parent.height * 0.12); width: parent.width * root.percent / 100
            color: Kirigami.Theme.highlightColor; radius: height / 2
        }
    }

    fullRepresentation: ColumnLayout {
        spacing: Kirigami.Units.smallSpacing
        Layout.minimumWidth: Kirigami.Units.gridUnit * 20
        Layout.minimumHeight: Kirigami.Units.gridUnit * 9

        Kirigami.Heading { level: 3; text: "Encode worker: " + root.headline; Layout.fillWidth: true; elide: Text.ElideRight }
        PlasmaComponents3.Label {
            visible: root.alive && root.status.name
            text: root.status ? root.status.name + "  →  " + (root.status.sidecar || "") : ""
            opacity: 0.7; Layout.fillWidth: true; elide: Text.ElideMiddle
        }

        PlasmaComponents3.Label { visible: root.encoding; text: root.encoding ? root.status.file : ""; Layout.fillWidth: true; elide: Text.ElideMiddle }
        PlasmaComponents3.ProgressBar { visible: root.encoding; from: 0; to: 100; value: root.percent; Layout.fillWidth: true }
        PlasmaComponents3.Label {
            visible: root.encoding
            Layout.fillWidth: true
            text: root.encoding ? [Math.round(root.percent) + " %",
                                   root.status.speed_x ? root.status.speed_x.toFixed(1) + "× speed" : "",
                                   root.fmtLeft(root.status.seconds_left) ? root.fmtLeft(root.status.seconds_left) + " left" : "",
                                   root.status.method || ""].filter(function (x) { return x }).join("  ·  ") : ""
        }
        PlasmaComponents3.Label {
            visible: root.alive && root.status.state === "unreachable"
            text: root.alive ? (root.status.error || "") : ""
            color: Kirigami.Theme.negativeTextColor; wrapMode: Text.Wrap; Layout.fillWidth: true
        }
        PlasmaComponents3.Label {
            visible: root.alive && root.status.state === "idle"
            text: "Asking the sidecar for work every " + (root.status ? (root.status.interval || 30) : 30) + " s"
            opacity: 0.7; Layout.fillWidth: true
        }
        PlasmaComponents3.Label {
            visible: root.showLast
            Layout.fillWidth: true; wrapMode: Text.Wrap; opacity: 0.85
            color: root.status && root.status.last_result && !root.status.last_result.ok ? Kirigami.Theme.negativeTextColor : Kirigami.Theme.textColor
            text: root.status && root.status.last_result ? "Last: " + root.status.last_result.file + " — " + root.status.last_result.text + " (" + root.fmtAgo(root.status.last_result.at) + ")" : ""
        }
        PlasmaComponents3.Label {
            visible: root.byHand
            text: "Started by hand in a terminal: stop it there (Ctrl+C); Start/Stop here control the systemd service."
            wrapMode: Text.Wrap; Layout.fillWidth: true; color: Kirigami.Theme.neutralTextColor
        }

        PlasmaComponents3.CheckBox {
            id: autoBox
            text: "Work through the whole Convert list (queue)"
            enabled: !root.busy && root.alive
            checked: root.autoQueue
            onToggled: root.setAutoQueue(checked)
            Connections { target: root; function onAutoQueueChanged() { autoBox.checked = root.autoQueue } }
        }
        Item { Layout.fillHeight: true }
        RowLayout {
            Layout.fillWidth: true
            PlasmaComponents3.Button {
                text: "Start"; icon.name: "media-playback-start"
                enabled: !root.busy && !root.alive
                onClicked: root.control("start")
            }
            PlasmaComponents3.Button {
                text: "Stop"; icon.name: "media-playback-stop"
                enabled: !root.busy && root.serviceState === "active"
                onClicked: root.control("stop")
            }
            PlasmaComponents3.Button {
                text: "Restart"; icon.name: "view-refresh"
                enabled: !root.busy && root.serviceState === "active"
                onClicked: root.control("restart")
            }
        }
    }
}
