import QtQuick
import QtQuick.Controls.Basic
import QtQuick.Layouts
import QtQuick.Dialogs as Dialogs

ApplicationWindow {
    id: win
    required property var backend
    property string page: "library"
    readonly property var session: backend.state
    property bool connectionExpanded: false
    readonly property bool paired: session.paired
    onPairedChanged: if (paired) connectionExpanded = false
    visible: true
    width: 1040
    height: 760
    minimumWidth: 780
    minimumHeight: 620
    title: "FrameSync"
    readonly property bool dark: backend.appearance === "system"
                                 ? Qt.styleHints.colorScheme === Qt.Dark : backend.appearance === "dark"
    Binding { target: FrameTheme; property: "dark"; value: win.dark }
    color: FrameTheme.canvas
    palette: FramePalette {}
    FontLoader { id: bodyFont; source: "fonts/atkinson.woff2" }
    FontLoader { id: headingFont; source: "fonts/asul.woff2" }
    font.family: bodyFont.name
    font.pixelSize: 15

    component Title: Label {
        font.family: headingFont.name
        color: FrameTheme.ink
    }
    onClosing: close => {
        if (win.session.busy) {
            close.accepted = false
            backend.stop()
        }
    }
    CropEditor {
        id: cropEditor
        objectName: "cropEditor"
        backend: win.backend
    }
    Connections {
        target: win.backend
        function onMuseumPreviewReady(photo) {
            cropEditor.edit(-1, photo)
        }
    }
    Dialogs.FileDialog {
        id: files
        title: "Choose photos to send"
        fileMode: Dialogs.FileDialog.OpenFiles
        nameFilters: ["Photos (*.jpg *.jpeg *.png *.JPG *.JPEG *.PNG)"]
        onAccepted: backend.addPhotos(selectedFiles)
    }
    Dialogs.FolderDialog {
        id: chooser
        title: "Choose a photo folder"
        onAccepted: backend.chooseFolder(selectedFolder)
    }
    FrameDialog {
        id: retry
        objectName: "retryDialog"
        anchors.centerIn: parent
        width: 460
        modal: true
        title: "Check your TV before retrying"
        footer: DialogButtonBox {
            FrameButton { text: "Cancel"; DialogButtonBox.buttonRole: DialogButtonBox.RejectRole }
            FrameButton { objectName: "confirmRetry"; text: "Retry"; DialogButtonBox.buttonRole: DialogButtonBox.AcceptRole }
        }
        contentItem: Label {
            wrapMode: Text.WordWrap
            textFormat: Text.PlainText
            text: "Check My Photos on the TV for " + win.session.uncertain.join(", ") +
                  ". Remove any unconfirmed copy before retrying. Confirmed photos will be skipped."
        }
        onAccepted: backend.resolve()
    }
    RowLayout {
        anchors.fill: parent
        spacing: 0
        Rectangle {
            Layout.fillHeight: true
            Layout.preferredWidth: 226
            color: FrameTheme.side
            border.color: FrameTheme.line
            ColumnLayout {
                anchors.fill: parent
                anchors.margins: 18
                spacing: 9
                RowLayout {
                    spacing: 8
                    Layout.fillWidth: true
                    Image {
                        source: "relay-mark.svg"
                        Layout.preferredWidth: 41
                        Layout.preferredHeight: 28
                        fillMode: Image.PreserveAspectFit
                    }
                    Label {
                        text: "FrameSync"
                        color: FrameTheme.ink
                        font.pixelSize: 24
                        font.weight: Font.DemiBold
                    }
                }
                FrameButton {
                    text: "Local photos"
                    Layout.fillWidth: true
                    alignLeft: true
                    checked: win.page === "library"
                    enabled: !win.session.busy
                    onClicked: win.page = "library"
                }
                FrameButton {
                    objectName: "discover"
                    text: "Discover"
                    Layout.fillWidth: true
                    alignLeft: true
                    checked: win.page === "discover"
                    enabled: !win.session.busy
                    onClicked: win.page = "discover"
                }
                FrameButton {
                    objectName: "framePage"
                    text: "On the Frame"
                    Layout.fillWidth: true
                    alignLeft: true
                    checked: win.page === "frame"
                    enabled: !win.session.busy
                    onClicked: win.page = "frame"
                }
                Item { Layout.fillHeight: true }
                ColumnLayout {
                    id: setup
                    objectName: "connectionSetup"
                    Layout.fillWidth: true
                    visible: win.connectionExpanded || (!win.paired && !win.session.knownTarget)
                    spacing: 8
                    Label { text: "Your Frame"; font.bold: true }
                    Label { Layout.fillWidth: true; text: "Display a picture in Art Mode, then connect using the TV’s local IP address."; wrapMode: Text.WordWrap; color: FrameTheme.muted }
                    FrameTextField {
                        id: host
                        objectName: "host"
                        Layout.fillWidth: true
                        enabled: !win.session.busy
                        text: win.session.host
                        placeholderText: "192.168.1.50"
                        Accessible.name: "TV IP address"
                        onTextEdited: backend.setTarget(text, win.session.port)
                    }
                    FrameComboBox {
                        id: portCombo
                        Layout.fillWidth: true
                        enabled: !win.session.busy
                        Accessible.name: "Connection port"
                        model: ["Secure · 8002", "Legacy · 8001"]
                        currentIndex: win.session.port === 8001 ? 1 : 0
                        onActivated: backend.setTarget(host.text, currentIndex === 1 ? 8001 : 8002)
                    }
                    Connections {
                        target: win.backend
                        function onChanged() {
                            if (host.text !== win.session.host)
                                host.text = win.session.host
                            const portIndex = win.session.port === 8001 ? 1 : 0
                            if (portCombo.currentIndex !== portIndex)
                                portCombo.currentIndex = portIndex
                        }
                    }
                    FrameButton { objectName: "connect"; text: win.session.reconnecting ? "Reconnecting…" : "Connect"; enabled: !win.session.busy && !win.session.reconnecting; onClicked: backend.launch(false) }
                }
                FrameButton {
                    objectName: "myframeFitSidebarCard"
                    Layout.fillWidth: true
                    visible: win.page === "library"
                    horizontalPadding: 8
                    text: "Make art for your Frame"
                    Accessible.description: "Opens myframe.fit in your default browser"
                    contentItem: RowLayout {
                        spacing: 8
                        Image {
                            source: "myframe-fit-card.png"
                            // Use the dog thumbnail from the wide Discover banner.
                            sourceClipRect: Qt.rect(12, 12, 122, 76)
                            Layout.preferredWidth: 48
                            Layout.preferredHeight: 30
                            fillMode: Image.PreserveAspectFit
                        }
                        ColumnLayout {
                            Layout.fillWidth: true
                            spacing: 1
                            Label {
                                Layout.fillWidth: true
                                text: "Make art for\nyour Frame"
                                wrapMode: Text.WordWrap
                                font.pixelSize: 13
                                font.weight: Font.DemiBold
                                color: FrameTheme.ink
                            }
                            Label { text: "myframe.fit"; font.pixelSize: 12; color: FrameTheme.muted }
                        }
                        Label { text: "›"; font.pixelSize: 20; color: FrameTheme.violet }
                    }
                    HoverHandler { cursorShape: Qt.PointingHandCursor }
                    onClicked: Qt.openUrlExternally("https://myframe.fit")
                }
                RowLayout {
                    Layout.fillWidth: true
                    spacing: 2
                    FrameButton {
                        objectName: "connectionStatus"
                        font.pixelSize: 13
                        Layout.fillWidth: true
                        alignLeft: true
                        quiet: true
                        horizontalPadding: 0
                        text: win.paired ? "●  Connected" : win.session.reconnecting ? "○  Reconnecting…" : "○  Disconnected"
                        Accessible.name: text + ", connection details"
                        ToolTip.visible: hovered
                        ToolTip.text: "Connection details"
                        enabled: !win.session.busy && (win.session.knownTarget || win.paired)
                        onClicked: win.connectionExpanded = !win.connectionExpanded
                    }
                    FrameButton {
                        objectName: "retryConnection"
                        font.pixelSize: 13
                        quiet: true
                        horizontalPadding: 3
                        text: "Retry"
                        visible: !setup.visible && !win.paired && !win.session.reconnecting
                        enabled: !win.session.busy
                        onClicked: backend.launch(false)
                    }
                    FrameButton {
                        id: appearanceButton
                        objectName: "appearanceButton"
                        quiet: true
                        Layout.preferredWidth: 32
                        Layout.preferredHeight: 32
                        horizontalPadding: 0
                        text: "◐"
                        font.pixelSize: 20
                        Accessible.name: "Appearance"
                        Accessible.description: "Current setting: " + win.backend.appearance
                        ToolTip.visible: hovered
                        ToolTip.text: "Appearance · " + win.backend.appearance
                        onClicked: appearanceMenu.open()
                        Menu {
                            id: appearanceMenu
                            objectName: "appearanceMenu"
                            width: 142
                            x: appearanceButton.width - width
                            y: -height - 4
                            palette: FramePalette {}
                            component ModeItem: MenuItem {
                                required property string mode
                                objectName: "appearance-" + mode
                                checkable: true
                                autoExclusive: true
                                checked: win.backend.appearance === mode
                                onTriggered: win.backend.setAppearance(mode)
                            }
                            ModeItem { text: "System"; mode: "system" }
                            ModeItem { text: "Light"; mode: "light" }
                            ModeItem { text: "Dark"; mode: "dark" }
                        }
                    }
                }
            }
        }
        FrameGallery {
            Layout.fillWidth: true
            Layout.fillHeight: true
            Layout.margins: 24
            visible: win.page === "frame"
            backend: win.backend
        }
        Museum {
            Layout.fillWidth: true
            Layout.fillHeight: true
            Layout.margins: 26
            visible: win.page === "discover"
            backend: win.backend
            headingFamily: headingFont.name
        }
        ColumnLayout {
            visible: win.page === "library"
            Layout.fillWidth: true
            Layout.fillHeight: true
            Layout.margins: 26
            spacing: 14
            Label { text: "Library  /  Local photos"; color: FrameTheme.muted }
            RowLayout {
                Layout.fillWidth: true
                Title { text: "Your photos"; font.pixelSize: 38; Layout.fillWidth: true }
                FrameButton { objectName: "addPhotos"; text: "Add photos…"; enabled: !win.session.busy; onClicked: files.open() }
                FrameButton { text: "Choose folder…"; enabled: !win.session.busy; onClicked: chooser.open() }
            }
            Label {
                Layout.fillWidth: true
                elide: Text.ElideMiddle
                text: win.session.folder ? win.session.folderCount + (win.session.folderCount === 1 ? " photo  ·  " : " photos  ·  ") + win.session.folder : "Drop JPG or PNG files here, or choose photos from Finder."
                color: FrameTheme.muted
            }
            Rectangle {
                Layout.fillWidth: true
                Layout.fillHeight: true
                radius: 14
                color: FrameTheme.line
                Label {
                    anchors.centerIn: parent
                    visible: !backend.photos.length
                    text: win.session.folder ? "No JPG or PNG photos in this folder" : "Drop photos here"
                    color: FrameTheme.muted
                }
                GridView {
                    id: grid
                    anchors.fill: parent
                    anchors.margins: 10
                    clip: true
                    model: backend.photos
                    cellWidth: width / Math.max(1, Math.floor(width / 200))
                    cellHeight: cellWidth * 9 / 16 + 115
                    ScrollBar.vertical: ScrollBar {}
                    delegate: Item {
                        required property int index
                        required property var modelData
                        width: grid.cellWidth
                        height: grid.cellHeight
                        Rectangle {
                            anchors.fill: parent
                            anchors.margins: 5
                            color: FrameTheme.panel
                            radius: 9
                            Rectangle {
                                x: thumb.x; y: thumb.y; width: thumb.width; height: thumb.height
                                color: FrameTheme.softPanel
                            }
                            Image {
                                id: thumb
                                x: 6; y: 6
                                width: parent.width - 12; height: width * 9 / 16
                                visible: !modelData.cropped
                                source: visible ? modelData.url : ""
                                sourceSize: Qt.size(512, 512)
                                cache: false
                                autoTransform: true
                                asynchronous: true
                                fillMode: Image.PreserveAspectFit
                            }
                            Loader {
                                x: thumb.x; y: thumb.y; width: thumb.width; height: thumb.height
                                active: modelData.cropped
                                sourceComponent: CropPreview {
                                    photo: modelData
                                    zoom: modelData.framing[0]
                                    panX: modelData.framing[1]
                                    panY: modelData.framing[2]
                                }
                            }
                            CheckBox {
                                anchors.left: thumb.left
                                anchors.top: thumb.top
                                padding: 2
                                background: Rectangle { radius: 5; color: FrameTheme.panel }
                                Accessible.name: "Select " + modelData.name
                                enabled: !win.session.busy
                                checked: !!win.backend.selectedUrls[modelData.url]
                                onClicked: win.backend.selectPhoto(index, checked)
                            }
                            Label {
                                id: filename
                                anchors.top: thumb.bottom
                                anchors.left: thumb.left
                                anchors.right: thumb.right
                                topPadding: 8
                                text: modelData.name
                                textFormat: Text.PlainText
                                elide: Text.ElideMiddle
                            }
                            Label {
                                id: resolution
                                anchors.top: filename.bottom
                                anchors.left: thumb.left
                                anchors.topMargin: 4
                                padding: 3
                                font.pixelSize: 12
                                color: modelData.exact ? FrameTheme.green : FrameTheme.muted
                                background: Rectangle { radius: 4; color: modelData.exact ? FrameTheme.greenBg : "transparent" }
                                text: modelData.exact ? "✓ Exact 4K · 3840 × 2160" :
                                      modelData.w ? modelData.w + " × " + modelData.h + " px" : "Unreadable photo"
                            }
                            FrameButton {
                                anchors.top: resolution.bottom
                                anchors.topMargin: 4
                                anchors.left: thumb.left
                                implicitHeight: 30
                                visible: modelData.cropped || (!modelData.exact && modelData.w > 0)
                                enabled: !win.session.busy
                                text: modelData.cropped ? "Edit crop" : "Crop to 4K"
                                onClicked: cropEditor.edit(index, modelData)
                            }
                        }
                    }
                }
            }
            Label { Layout.fillWidth: true; wrapMode: Text.WordWrap; textFormat: Text.PlainText; text: win.session.message }
            ProgressBar {
                Layout.fillWidth: true
                visible: win.session.busy
                indeterminate: !win.session.total
                value: win.session.total ? win.session.done / win.session.total : 0
            }
            RowLayout {
                FrameButton { objectName: "recover"; visible: win.session.uncertain.length > 0; enabled: !win.session.busy; text: "Resolve interrupted upload…"; onClicked: retry.open() }
                Item { Layout.fillWidth: true }
                FrameButton { visible: win.session.syncing; text: "Stop after this photo"; onClicked: backend.stop() }
                FrameButton { objectName: "sendSelected"; visible: win.session.selectedCount > 0 && !win.session.busy
                    primary: true; text: "Send selected (" + win.session.selectedCount + ")"
                    enabled: win.session.paired && !win.session.uncertain.length
                    onClicked: win.backend.sendSelected()
                }
                FrameButton { objectName: "sync"; primary: !win.session.selectedCount; text: "Sync folder"; enabled: win.session.ready; onClicked: backend.launch(true) }
            }
        }
    }
    DropArea {
        anchors.fill: parent
        enabled: !win.session.busy && !cropEditor.visible && !retry.visible
        onEntered: drag => { drag.accepted = drag.hasUrls && win.page === "library" }
        onDropped: drop => {
            if (drop.hasUrls && win.page === "library") {
                win.backend.addPhotos(drop.urls)
                drop.acceptProposedAction()
            }
        }
    }
}
