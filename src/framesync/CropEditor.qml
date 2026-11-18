import QtQuick
import QtQuick.Controls.Basic
import QtQuick.Layouts
import QtQuick.Dialogs as Dialogs
import QtCore

FrameDialog {
    id: editor
    required property var backend
    property var photo: ({w: 1, h: 1, url: "", name: "", cropped: false})
    property int photoIndex: -1
    property bool museumPreview: false
    property bool savedCopy: false
    readonly property var session: backend ? backend.state : ({paired: false, busy: false, uncertain: []})
    readonly property bool saving: museumPreview && session.busy
    readonly property string connectionHint: !session.paired ? "Connect your Frame to send this crop."
        : session.uncertain.length ? "Resolve the interrupted upload in Local photos before sending." : ""
    function edit(row, item) {
        museumPreview = row < 0
        savedCopy = false
        photoIndex = row
        photo = item
        zoom.value = item.framing[0]
        horizontal.value = item.framing[1]
        vertical.value = item.framing[2]
        open()
    }
    anchors.centerIn: parent
    width: Math.min(610, parent.width - 48, (parent.height - (museumPreview ? 350 : 320)) * 16 / 9)
    modal: true
    closePolicy: saving ? Popup.NoAutoClose : Popup.CloseOnEscape | Popup.CloseOnPressOutside
    title: museumPreview ? "Preview & crop" : "Crop to 4K"
    onClosed: {
        if (museumPreview) {
            backend.discardMuseumPreview()
            museumPreview = false
        }
        photo = {w: 1, h: 1, url: "", name: "", cropped: false}
    }
    Connections {
        target: editor.backend
        function onMuseumPreviewSaved() { editor.savedCopy = true }
    }
    FrameDialog {
        id: connectDialog
        objectName: "cropConnection"
        parent: Overlay.overlay
        anchors.centerIn: parent
        width: Math.min(400, parent.width - 32)
        title: "Connect to Frame"
        modal: true
        property bool attempted: false
        closePolicy: editor.session.busy ? Popup.NoAutoClose : Popup.CloseOnEscape
        function configure() {
            attempted = false
            address.text = editor.session.host
            port.currentIndex = editor.session.port === 8001 ? 1 : 0
            open()
        }
        Connections {
            target: editor.backend
            function onChanged() {
                if (connectDialog.visible && editor.backend.state.paired)
                    connectDialog.close()
            }
        }
        contentItem: ColumnLayout {
            spacing: 8
            Label {
                Layout.fillWidth: true
                wrapMode: Text.WordWrap
                text: "Display a picture in Art Mode, then connect using your TV’s local IP address."
            }
            Label { text: "TV IP address" }
            FrameTextField {
                id: address
                objectName: "cropTVAddress"
                Layout.fillWidth: true
                Accessible.name: "TV IP address"
                placeholderText: "192.168.1.50"
                enabled: !editor.session.busy
                onAccepted: if (connectButton.enabled) connectButton.click()
            }
            FrameComboBox {
                id: port
                Layout.fillWidth: true
                Accessible.name: "Connection port"
                model: ["Secure · 8002", "Legacy · 8001"]
                enabled: !editor.session.busy
            }
            Label {
                objectName: "cropConnectionStatus"
                Layout.fillWidth: true
                visible: connectDialog.attempted
                text: visible ? editor.session.message : ""
                textFormat: Text.PlainText
                wrapMode: Text.WordWrap
            }
            RowLayout {
                Item { Layout.fillWidth: true }
                FrameButton {
                    text: "Back"
                    enabled: !editor.session.busy
                    onClicked: connectDialog.close()
                }
                FrameButton {
                    id: connectButton
                    objectName: "cropConnect"
                    text: editor.session.reconnecting ? "Reconnecting…" : editor.session.busy ? "Connecting…" : "Connect"
                    primary: true
                    enabled: !editor.session.busy && address.text.trim().length > 0 &&
                             (!editor.session.reconnecting || address.text.trim() !== editor.session.host ||
                              (port.currentIndex === 1 ? 8001 : 8002) !== editor.session.port)
                    onClicked: {
                        connectDialog.attempted = true
                        editor.backend.setTarget(address.text.trim(), port.currentIndex === 1 ? 8001 : 8002)
                        editor.backend.launch(false)
                    }
                }
            }
        }
    }
    Dialogs.FileDialog {
        id: saveCopy
        objectName: "museumSaveDialog"
        title: "Save cropped artwork"
        fileMode: Dialogs.FileDialog.SaveFile
        nameFilters: ["JPEG image (*.jpg *.jpeg)"]
        defaultSuffix: "jpg"
        onAccepted: editor.backend.saveMuseumPreview(selectedFile, zoom.value, horizontal.value, vertical.value)
    }
    contentItem: ColumnLayout {
        spacing: 8
        Label { Layout.fillWidth: true; text: editor.photo.name; textFormat: Text.PlainText; elide: Text.ElideMiddle }
        CropPreview {
            id: preview
            Layout.fillWidth: true
            Layout.preferredHeight: width * 9 / 16
            photo: editor.photo
            zoom: zoom.value
            panX: horizontal.value
            panY: vertical.value
            MouseArea {
                anchors.fill: parent
                enabled: !editor.saving
                property point start
                property point original
                onPressed: mouse => {
                    start = Qt.point(mouse.x, mouse.y)
                    original = Qt.point(horizontal.value, vertical.value)
                }
                onPositionChanged: mouse => {
                    if (!pressed) return
                    if (preview.overflowX > 0.01)
                        horizontal.value = original.x - (mouse.x - start.x) / preview.overflowX
                    if (preview.overflowY > 0.01)
                        vertical.value = original.y - (mouse.y - start.y) / preview.overflowY
                }
            }
        }
        Label {
            text: preview.failed ? "Could not open this image." : "3840 × 2160 px · 16:9" +
                (Math.min(editor.photo.w, editor.photo.h * 16 / 9) / zoom.value < 3839.99 ? " · Upscaled" : "")
        }
        GridLayout {
            enabled: !editor.saving
            Layout.fillWidth: true
            columns: 2
            Label { text: "Zoom" }
            Slider { id: zoom; objectName: "cropZoom"; Accessible.name: "Crop zoom"; Layout.fillWidth: true; from: 1; to: 3 }
            Label { text: "Horizontal position" }
            Slider { id: horizontal; Accessible.name: "Horizontal position"; Layout.fillWidth: true; enabled: preview.overflowX > 0.01 }
            Label { text: "Vertical position" }
            Slider { id: vertical; Accessible.name: "Vertical position"; Layout.fillWidth: true; enabled: preview.overflowY > 0.01 }
        }
        Label {
            Layout.fillWidth: true
            visible: editor.museumPreview && text.length > 0
            text: editor.museumPreview && editor.backend ? (editor.backend.collection.message || editor.connectionHint) : ""
            textFormat: Text.PlainText
            wrapMode: Text.WordWrap
        }
        RowLayout {
            Layout.fillWidth: true
            FrameButton {
                visible: !editor.museumPreview && editor.photo.cropped
                text: "Use whole photo"
                onClicked: { editor.backend.applyCrop(editor.photoIndex, 1, 0.5, 0.5, true); editor.close() }
            }
            Item { Layout.fillWidth: true }
            FrameButton { text: editor.savedCopy ? "Done" : "Cancel"; enabled: !editor.saving; onClicked: editor.reject() }
            FrameButton {
                objectName: "saveMuseumCopy"
                visible: editor.museumPreview
                enabled: preview.ready && !editor.saving
                text: "Save a copy…"
                onClicked: {
                    const folder = StandardPaths.writableLocation(StandardPaths.PicturesLocation)
                    const name = editor.photo.name.replace(/[\/:]/g, "-").replace(/\.jpe?g$/i, "")
                    saveCopy.selectedFile = folder + "/" + encodeURIComponent(name + ".jpg")
                    saveCopy.open()
                }
            }
            FrameButton {
                objectName: "sendMuseumCrop"
                visible: editor.museumPreview
                enabled: preview.ready && !editor.saving && (!editor.session.paired || !editor.connectionHint)
                primary: true
                text: editor.session.paired ? "Send to Frame" : "Connect to Frame…"
                onClicked: {
                    if (!editor.session.paired) {
                        connectDialog.configure()
                        return
                    }
                    if (editor.backend.sendMuseumPreview(zoom.value, horizontal.value, vertical.value)) {
                        editor.museumPreview = false
                        editor.close()
                    }
                }
            }
            FrameButton {
                objectName: "useCrop"
                visible: !editor.museumPreview
                enabled: preview.ready
                text: "Use 4K crop"
                onClicked: {
                    editor.backend.applyCrop(editor.photoIndex, zoom.value, horizontal.value, vertical.value, false)
                    editor.close()
                }
            }
        }
    }
}
