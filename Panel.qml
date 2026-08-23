import QtQuick
import QtQuick.Controls
import Quickshell
import Quickshell.Io
import Quickshell.Services.Pipewire
import qs.Ui
import qs.Commons
import "Model.js" as Model

Panel {
  id: root
  moduleName: "tbye.sonos"
  ipcTarget: "tbye.sonos"

  readonly property string helper: {
    var path = String(Qt.resolvedUrl("sonosctl.py")).replace(/^file:\/\//, "")
    try { return decodeURIComponent(path) } catch (e) { return path }
  }

  property var speakers: []
  property var localOutput: ({
    id: "local",
    name: "This computer",
    description: "Built-in Audio",
    selected: true,
    sinkName: "",
    sinkId: ""
  })
  property string outputId: "local"
  property bool raopReady: false
  property string lastError: ""
  property bool loading: false
  property string pendingSelect: ""
  property string draggingUid: ""
  property string queuedUid: ""
  property int queuedVolume: -1
  property real wheelAccumulator: 0
  property int selectedIndex: 0
  property bool cursorActive: false
  property string statusText: "Looking for speakers"

  readonly property var nodes: Pipewire.nodes ? Pipewire.nodes.values : []
  readonly property var sinkNodes: {
    var list = []
    for (var i = 0; i < nodes.length; i++) {
      var n = nodes[i]
      if (n && n.isSink && !n.isStream) list.push(n)
    }
    return list
  }

  readonly property color foreground: bar ? bar.foreground : Color.foreground
  readonly property color dim: Qt.darker(foreground, 1.45)
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family
  readonly property color hoverFill: bar ? Style.hoverFillFor(bar.foreground, Color.accent) : "transparent"
  readonly property color selectedFill: bar ? Style.selectedFillFor(bar.foreground, Color.accent) : "transparent"
  readonly property bool routingToSonos: outputId !== "" && outputId !== "local"
  readonly property int rowCount: 1 + speakers.length
  readonly property var selectedSpeaker: speakerByUid(outputId)
  readonly property string currentName: routingToSonos && selectedSpeaker
    ? selectedSpeaker.name
    : (localOutput.name || "This computer")
  readonly property string heroMeta: loading && speakers.length === 0
    ? "SEARCHING"
    : (lastError && speakers.length === 0 ? "UNREACHABLE" : currentName.toUpperCase())

  PwObjectTracker { objects: root.sinkNodes }

  function speakerByUid(uid) {
    for (var i = 0; i < speakers.length; i++)
      if (speakers[i].uid === uid) return speakers[i]
    return null
  }

  function compact(text) {
    return String(text || "").toLowerCase().replace(/[:_\-\s]/g, "")
  }

  function nodeBlob(node) {
    if (!node) return ""
    return [node.name, node.description, node.nickname].join(" ")
  }

  function isRaopNode(node) {
    return compact(nodeBlob(node)).indexOf("raop") !== -1
      || compact(nodeBlob(node)).indexOf("airplay") !== -1
  }

  function sinkForSpeaker(spk) {
    if (!spk) return null
    var mac = compact(spk.mac)
    var name = String(spk.name || "").toLowerCase()
    var named = String(spk.sinkName || "")
    var namedHit = null
    var macHit = null
    var sonosNameHit = null
    for (var i = 0; i < sinkNodes.length; i++) {
      var n = sinkNodes[i]
      if (!n) continue
      if (named && String(n.name) === named) namedHit = namedHit || n
      var blob = compact(nodeBlob(n))
      if (mac && blob.indexOf(mac) !== -1) {
        macHit = n
        break
      }
      if (isRaopNode(n) && blob.indexOf("sonos") !== -1 && name
          && String(nodeBlob(n)).toLowerCase().indexOf(name) !== -1)
        sonosNameHit = sonosNameHit || n
    }
    return macHit || namedHit || sonosNameHit
  }

  function localSinkNode() {
    var named = String(localOutput.sinkName || "")
    if (named) {
      for (var i = 0; i < sinkNodes.length; i++)
        if (sinkNodes[i] && String(sinkNodes[i].name) === named) return sinkNodes[i]
    }
    for (var j = 0; j < sinkNodes.length; j++) {
      var n = sinkNodes[j]
      if (!n || isRaopNode(n)) continue
      var blob = String(nodeBlob(n)).toLowerCase()
      if (blob.indexOf("analog") !== -1 || blob.indexOf("built-in") !== -1) return n
    }
    for (var k = 0; k < sinkNodes.length; k++)
      if (sinkNodes[k] && !isRaopNode(sinkNodes[k])) return sinkNodes[k]
    return null
  }

  function setDefaultSink(node) {
    if (!node) return false
    Pipewire.preferredDefaultAudioSink = node
    if (node.id !== undefined && node.name) {
      Quickshell.execDetached([
        "omarchy-audio-output-set-default",
        String(node.id),
        String(node.name)
      ])
    }
    return true
  }

  function refresh() {
    if (listProc.running) return
    loading = speakers.length === 0
    listProc.running = true
  }

  function applyList(raw) {
    var parsed = Model.parseList(raw)
    loading = false
    if (!parsed.ok) {
      lastError = parsed.error || "Sonos lookup failed"
      if (speakers.length === 0)
        statusText = lastError
      return
    }
    lastError = ""
    localOutput = parsed.local
    raopReady = parsed.raopReady === true
    outputId = parsed.outputId || "local"
    var next = []
    for (var i = 0; i < parsed.speakers.length; i++) {
      var row = parsed.speakers[i]
      if (draggingUid && row.uid === draggingUid) {
        var keep = speakerByUid(draggingUid)
        if (keep) {
          row = Object.assign({}, row)
          row.volume = keep.volume
        }
      }
      next.push(row)
    }
    speakers = next
    statusText = speakers.length === 0
      ? "No speakers found on this network"
      : (routingToSonos ? ("Playing on " + currentName) : "This computer")
    if (selectedIndex >= rowCount) selectedIndex = Math.max(0, rowCount - 1)
    if (pendingSelect) tryPendingSelect()
  }

  function patchSpeaker(uid, fields) {
    var next = []
    for (var i = 0; i < speakers.length; i++) {
      var row = speakers[i]
      if (row.uid !== uid) {
        next.push(row)
        continue
      }
      var copy = Object.assign({}, row)
      for (var key in fields) copy[key] = fields[key]
      next.push(copy)
    }
    speakers = next
  }

  function selectLocal() {
    pendingSelect = ""
    outputId = "local"
    localOutput = Object.assign({}, localOutput, { selected: true })
    var next = []
    for (var i = 0; i < speakers.length; i++)
      next.push(Object.assign({}, speakers[i], { selected: false }))
    speakers = next
    setDefaultSink(localSinkNode())
    Quickshell.execDetached(["python3", "-B", helper, "set-output", "local"])
    statusText = "This computer"
    delayedRefresh.restart()
  }

  function selectSpeaker(spk) {
    if (!spk || spk.selectable === false) return
    outputId = spk.uid
    pendingSelect = spk.uid
    localOutput = Object.assign({}, localOutput, { selected: false })
    var next = []
    for (var i = 0; i < speakers.length; i++)
      next.push(Object.assign({}, speakers[i], { selected: speakers[i].uid === spk.uid }))
    speakers = next
    var node = sinkForSpeaker(spk)
    if (node) {
      setDefaultSink(node)
      pendingSelect = ""
    }
    Quickshell.execDetached(["python3", "-B", helper, "set-output", spk.uid])
    statusText = "Playing on " + spk.name
    delayedRefresh.restart()
    pendingTimer.restart()
  }

  function tryPendingSelect() {
    if (!pendingSelect) return
    var spk = speakerByUid(pendingSelect)
    var node = sinkForSpeaker(spk)
    if (!node) return
    setDefaultSink(node)
    pendingSelect = ""
    pendingTimer.stop()
    delayedRefresh.restart()
  }

  function queueVolume(uid, level) {
    var value = Math.max(0, Math.min(100, Math.round(level)))
    var current = speakerByUid(uid)
    patchSpeaker(uid, { volume: value, muted: current ? current.muted : false })
    queuedUid = uid
    queuedVolume = value
    volumeFlush.restart()
  }

  function flushVolume() {
    if (!queuedUid || queuedVolume < 0) return
    Quickshell.execDetached([
      "python3", "-B", helper, "volume", queuedUid, String(queuedVolume)
    ])
    queuedUid = ""
    queuedVolume = -1
  }

  function toggleMute(uid) {
    var spk = speakerByUid(uid)
    if (!spk) return
    patchSpeaker(uid, { muted: !spk.muted })
    Quickshell.execDetached(["python3", "-B", helper, "mute", uid, "toggle"])
    delayedRefresh.restart()
  }

  function moveCursor(delta) {
    if (rowCount <= 0) return
    selectedIndex = Math.max(0, Math.min(rowCount - 1, selectedIndex + delta))
    ensureCursorVisible()
  }

  function activateCursor() {
    if (selectedIndex <= 0) {
      selectLocal()
      return
    }
    var spk = speakers[selectedIndex - 1]
    if (spk) selectSpeaker(spk)
  }

  function adjustCursorVolume(delta) {
    if (selectedIndex <= 0) return
    var spk = speakers[selectedIndex - 1]
    if (!spk) return
    queueVolume(spk.uid, Number(spk.volume || 0) + delta)
  }

  function ensureCursorVisible() {
    if (!panelFlick) return
    var item = null
    if (selectedIndex <= 0) item = localRow
    else if (speakerColumn && selectedIndex - 1 < speakerColumn.children.length)
      item = speakerColumn.children[selectedIndex - 1]
    if (!item) return
    Qt.callLater(function() {
      if (!item || !panelFlick) return
      var margin = Style.space(6)
      var point = item.mapToItem(panelFlick.contentItem, 0, 0)
      var top = point.y
      var bottom = top + item.height
      var viewTop = panelFlick.contentY
      var viewBottom = viewTop + panelFlick.height
      var maxY = Math.max(0, panelFlick.contentHeight - panelFlick.height)
      if (top < viewTop + margin) panelFlick.contentY = Math.max(0, top - margin)
      else if (bottom > viewBottom - margin)
        panelFlick.contentY = Math.min(maxY, bottom + margin - panelFlick.height)
    })
  }

  function selectedOutputVolume(delta) {
    if (routingToSonos && selectedSpeaker) {
      queueVolume(selectedSpeaker.uid, Number(selectedSpeaker.volume || 0) + delta)
      return
    }
    var sink = Pipewire.defaultAudioSink
    if (sink && sink.audio)
      sink.audio.volume = Math.max(0, Math.min(1, sink.audio.volume + delta / 100))
  }

  onOpenedChanged: {
    if (opened) {
      cursorActive = false
      selectedIndex = 0
      for (var i = 0; i < speakers.length; i++) {
        if (speakers[i].uid === outputId) selectedIndex = i + 1
      }
      if (panelFlick) panelFlick.contentY = 0
      refresh()
    } else {
      draggingUid = ""
      flushVolume()
    }
  }

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  Process {
    id: listProc
    command: ["python3", "-B", root.helper, "list"]
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: root.applyList(text)
    }
    stderr: StdioCollector {
      waitForEnd: true
    }
  }

  Timer {
    interval: root.opened ? 4000 : 15000
    running: true
    repeat: true
    triggeredOnStart: true
    onTriggered: root.refresh()
  }

  Timer {
    id: delayedRefresh
    interval: 450
    repeat: false
    onTriggered: root.refresh()
  }

  Timer {
    id: volumeFlush
    interval: 90
    repeat: false
    onTriggered: root.flushVolume()
  }

  Timer {
    id: pendingTimer
    interval: 1500
    repeat: true
    onTriggered: {
      if (!root.pendingSelect) {
        stop()
        return
      }
      root.tryPendingSelect()
      if (!listProc.running) root.refresh()
    }
  }

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: "󰓃"
    active: root.routingToSonos
    tooltipText: root.statusText + "\nLeft-click: choose speaker   Right-click: this computer"

    onPressed: function(b) {
      if (b === Qt.RightButton) root.selectLocal()
      else root.toggle()
    }

    onWheelMoved: function(delta) {
      var wheel = Util.wheelSteps(root.wheelAccumulator, delta)
      root.wheelAccumulator = wheel.remainder
      if (wheel.steps === 0) return
      root.selectedOutputVolume(wheel.steps * 2)
    }
  }

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(380))
    contentHeight: panel.fittedContentHeight(column.implicitHeight, Style.space(560))

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      onMoveRequested: function(dx, dy) {
        if (!root.cursorActive) { root.cursorActive = true; return }
        if (dy !== 0) root.moveCursor(dy)
        else if (dx !== 0) root.adjustCursorVolume(dx * 2)
      }
      onActivateRequested: if (root.cursorActive) root.activateCursor()
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }
      onTextKey: function(t) {
        if (t === "r" || t === "R") root.refresh()
        else if (t === "m" || t === "M") {
          if (root.selectedIndex > 0 && root.speakers[root.selectedIndex - 1])
            root.toggleMute(root.speakers[root.selectedIndex - 1].uid)
        }
      }

      Flickable {
        id: panelFlick
        anchors.fill: parent
        contentWidth: width
        contentHeight: column.implicitHeight
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        flickableDirection: Flickable.VerticalFlick
        interactive: contentHeight > height
        ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

        Column {
          id: column
          width: panelFlick.width
          spacing: Style.space(12)

          PanelHero {
            width: parent.width
            title: "Sonos"
            meta: root.heroMeta
            foreground: root.foreground
            fontFamily: root.fontFamily
            iconOpacity: root.routingToSonos ? 1.0 : 0.7
            iconComponent: Component {
              Text {
                text: "󰓃"
                color: root.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.display
              }
            }
          }

          PanelSeparator { foreground: root.foreground }

          Column {
            width: parent.width
            spacing: Style.space(8)

            PanelSectionHeader {
              text: "OUTPUT"
              foreground: root.foreground
              fontFamily: root.fontFamily
            }

            CursorSurface {
              id: localRow
              width: parent.width
              implicitHeight: localInner.implicitHeight + Style.spacing.xl
              hasCursor: root.cursorActive && root.selectedIndex === 0
              current: root.outputId === "local"
              foreground: root.foreground
              fill: root.hoverFill
              currentFill: root.selectedFill

              Column {
                id: localInner
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
                anchors.leftMargin: Style.space(6)
                anchors.rightMargin: Style.space(6)
                spacing: Style.space(2)

                Row {
                  width: parent.width
                  spacing: Style.space(8)

                  Text {
                    text: "󰓃"
                    color: root.foreground
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.title
                    width: Style.space(22)
                    horizontalAlignment: Text.AlignHCenter
                    anchors.verticalCenter: parent.verticalCenter
                  }

                  Column {
                    width: parent.width - Style.space(30)
                    spacing: 0
                    anchors.verticalCenter: parent.verticalCenter

                    Text {
                      text: root.localOutput.name || "This computer"
                      color: root.foreground
                      font.family: root.fontFamily
                      font.pixelSize: Style.font.body
                      font.bold: root.outputId === "local"
                      elide: Text.ElideRight
                      width: parent.width
                    }

                    Text {
                      text: root.localOutput.description || "Built-in Audio"
                      color: root.dim
                      font.family: root.fontFamily
                      font.pixelSize: Style.font.caption
                      elide: Text.ElideRight
                      width: parent.width
                    }
                  }
                }
              }

              MouseArea {
                anchors.fill: parent
                hoverEnabled: true
                cursorShape: Qt.PointingHandCursor
                onContainsMouseChanged: if (containsMouse) {
                  root.cursorActive = true
                  root.selectedIndex = 0
                }
                onClicked: root.selectLocal()
              }
            }
          }

          PanelSeparator { foreground: root.foreground }

          Column {
            width: parent.width
            spacing: Style.space(8)

            Item {
              width: parent.width
              implicitHeight: speakersHeader.implicitHeight

              PanelSectionHeader {
                id: speakersHeader
                text: "SPEAKERS"
                foreground: root.foreground
                fontFamily: root.fontFamily
                anchors.left: parent.left
                anchors.verticalCenter: parent.verticalCenter
              }

              Text {
                visible: root.loading
                text: "SCANNING"
                color: root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                font.bold: true
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
              }
            }

            Text {
              visible: !root.loading && root.speakers.length === 0
              width: parent.width
              wrapMode: Text.WordWrap
              text: root.lastError || "No Sonos speakers found on this network"
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.body
            }

            Column {
              id: speakerColumn
              width: parent.width
              spacing: Style.space(6)

              Repeater {
                model: root.speakers

                SpeakerRow {
                  required property var modelData
                  required property int index
                  width: speakerColumn.width
                  speaker: modelData
                  rowIndex: index + 1
                }
              }
            }
          }
        }
      }
    }
  }

  component SpeakerRow: CursorSurface {
    id: row
    required property var speaker
    required property int rowIndex

    readonly property bool isActive: speaker && speaker.selected === true
    readonly property bool canSelect: !speaker || speaker.selectable !== false
    readonly property int volume: speaker ? Number(speaker.volume || 0) : 0
    readonly property bool muted: speaker ? speaker.muted === true : false

    hasCursor: root.cursorActive && root.selectedIndex === rowIndex
    onHasCursorChanged: if (hasCursor) root.ensureCursorVisible()
    current: isActive
    foreground: root.foreground
    fill: root.hoverFill
    currentFill: root.selectedFill
    implicitHeight: inner.implicitHeight + Style.spacing.xl

    Column {
      id: inner
      anchors.left: parent.left
      anchors.right: parent.right
      anchors.verticalCenter: parent.verticalCenter
      anchors.leftMargin: Style.space(6)
      anchors.rightMargin: Style.space(6)
      spacing: Style.space(4)

      Item {
        width: parent.width
        implicitHeight: nameRow.implicitHeight

        MouseArea {
          anchors.fill: parent
          hoverEnabled: true
          cursorShape: row.canSelect ? Qt.PointingHandCursor : Qt.ArrowCursor
          onClicked: if (row.canSelect) root.selectSpeaker(row.speaker)
        }

        Row {
          id: nameRow
          width: parent.width
          spacing: Style.space(8)

          Text {
            text: row.muted ? "󰝟" : "󰓃"
            color: root.foreground
            font.family: root.fontFamily
            font.pixelSize: Style.font.title
            width: Style.space(22)
            horizontalAlignment: Text.AlignHCenter
            anchors.verticalCenter: parent.verticalCenter
            opacity: row.muted ? 0.5 : 1.0

            MouseArea {
              anchors.fill: parent
              cursorShape: Qt.PointingHandCursor
              onClicked: if (row.speaker) root.toggleMute(row.speaker.uid)
            }
          }

          Text {
            text: row.speaker ? row.speaker.name : ""
            color: root.foreground
            font.family: root.fontFamily
            font.pixelSize: Style.font.body
            font.bold: row.isActive
            elide: Text.ElideRight
            width: parent.width - Style.space(22) - pct.width - Style.space(16)
            anchors.verticalCenter: parent.verticalCenter
            opacity: row.canSelect ? 1.0 : 0.7
          }

          Text {
            id: pct
            text: row.muted ? "MUTE" : (Math.round(row.volume) + "%")
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            font.bold: true
            width: Style.space(40)
            horizontalAlignment: Text.AlignRight
            anchors.verticalCenter: parent.verticalCenter
          }
        }
      }

      PanelSlider {
        bar: root.bar
        width: parent.width
        minimum: 0
        maximum: 100
        step: 2
        integer: true
        value: row.volume
        opacity: row.muted ? 0.45 : 1.0

        onMoved: function(v) {
          if (!row.speaker) return
          root.draggingUid = row.speaker.uid
          root.queueVolume(row.speaker.uid, v)
        }
        onReleased: function(v) {
          if (!row.speaker) return
          root.queueVolume(row.speaker.uid, v)
          root.flushVolume()
          root.draggingUid = ""
        }
        onRightClicked: if (row.speaker) root.toggleMute(row.speaker.uid)
      }
    }

    MouseArea {
      anchors.fill: parent
      hoverEnabled: true
      acceptedButtons: Qt.NoButton
      onContainsMouseChanged: if (containsMouse) {
        root.cursorActive = true
        root.selectedIndex = row.rowIndex
      }
    }
  }
}
