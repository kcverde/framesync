import QtQuick
import QtQuick.Controls.Basic
import QtQuick.Layouts
import QtQuick.Dialogs as Dialogs

ColumnLayout {
    id: page
    required property var backend
    required property string headingFamily
    readonly property var collection: backend ? backend.collection : ({works: [], query: "", message: "", more: false, busy: false})
    readonly property var session: backend ? backend.state : ({busy: false})
    spacing: 12

    readonly property string sourceId: ["met", "cleveland", "chicago"][source.currentIndex] || "met"

    function runSearch() {
        page.backend.searchMuseum(query.text, {
            source: page.sourceId,
            field: field.currentText, artist: artist.text.trim(), medium: medium.text.trim(),
            region: region.text.trim(), department: department.text.trim(),
            start: startYear.text, end: endYear.text, orientation: orientation.currentText, fits: fits.checked
        }, false)
    }

    Connections {
        target: page.backend
        function onMuseumFileNeeded(key, link, title) {
            localImport.workKey = key
            localImport.recordLink = link
            localImport.workTitle = title
            localImport.open()
        }
    }
    FrameDialog {
        id: localImport
        property string workKey
        property string recordLink
        property string workTitle
        title: "Import a downloaded image"
        parent: Overlay.overlay
        anchors.centerIn: parent
        width: Math.min(parent.width - 32, 450)
        modal: true
        standardButtons: Dialog.Close
        contentItem: ColumnLayout {
            Label {
                Layout.fillWidth: true
                text: "The image download for “" + localImport.workTitle + "” is unavailable. Save a copy from the museum record, then choose it here to crop."
                textFormat: Text.PlainText
                wrapMode: Text.WordWrap
            }
            FrameButton { text: "Open museum record ↗"; onClicked: Qt.openUrlExternally(localImport.recordLink) }
            FrameButton { text: "Choose image…"; onClicked: localImage.open() }
        }
    }
    Dialogs.FileDialog {
        id: localImage
        title: "Choose the downloaded artwork"
        nameFilters: ["Images (*.jpg *.jpeg *.png *.tif *.tiff *.JPG *.JPEG *.PNG *.TIF *.TIFF)"]
        onAccepted: {
            localImport.close()
            page.backend.importMuseumFile(localImport.workKey, selectedFile)
        }
    }

    FrameDialog {
        id: filters
        objectName: "museumFilters"
        title: "Search filters"
        modal: true
        parent: Overlay.overlay
        anchors.centerIn: parent
        width: Math.min(parent.width - 32, 500)
        standardButtons: Dialog.Apply | Dialog.Close
        onApplied: { close(); page.runSearch() }
        contentItem: GridLayout {
            columns: 2
            Label { text: "Search in" }
            FrameComboBox {
                id: field; Accessible.name: "Search in"
                model: source.currentIndex === 1 ? ["Keyword", "Title"] : ["Keyword", "Title", "Subject"]
                Layout.fillWidth: true
            }
            Label { text: "Artist" }
            FrameTextField { id: artist; Accessible.name: "Artist"; Layout.fillWidth: true }
            Label { text: "Medium" }
            FrameTextField { id: medium; Accessible.name: "Medium"; Layout.fillWidth: true }
            Label { text: "Department" }
            FrameTextField { id: department; Accessible.name: "Department"; Layout.fillWidth: true }
            Label { text: source.currentIndex === 1 ? "Culture or place" : "Place" }
            FrameTextField { id: region; Accessible.name: "Culture or place"; Layout.fillWidth: true }
            Label { text: "Years" }
            RowLayout {
                Layout.fillWidth: true
                FrameTextField { id: startYear; Layout.fillWidth: true; Layout.preferredWidth: 80; placeholderText: "From"; Accessible.name: "From year" }
                FrameTextField { id: endYear; Layout.fillWidth: true; Layout.preferredWidth: 80; placeholderText: "Through"; Accessible.name: "Through year" }
            }
            Label { text: "Orientation" }
            FrameComboBox { id: orientation; Accessible.name: "Orientation"; model: ["Any", "Landscape", "Portrait", "Square"]; Layout.fillWidth: true }
            Item {}
            CheckBox { id: fits; text: "Fits 4K without upscaling" }
            FrameButton {
                text: "Clear filters"
                Layout.columnSpan: 2
                onClicked: {
                    field.currentIndex = 0; artist.clear(); medium.clear(); region.clear()
                    department.clear(); startYear.clear(); endYear.clear()
                    orientation.currentIndex = 0; fits.checked = false
                }
            }
        }
    }

    RowLayout {
        Layout.fillWidth: true
        Label { text: "Discover"; font.family: page.headingFamily; font.pixelSize: 38; Layout.fillWidth: true }
        Button {
            objectName: "myframeFitCard"
            text: "Make art for your Frame"
            Accessible.description: "Opens myframe.fit in your default browser"
            implicitWidth: 320
            implicitHeight: 50
            padding: 0
            background: null
            contentItem: Image {
                source: "myframe-fit-card.png"
                fillMode: Image.PreserveAspectFit
            }
            HoverHandler { cursorShape: Qt.PointingHandCursor }
            Rectangle {
                anchors.fill: parent
                anchors.margins: -3
                radius: 13
                color: "transparent"
                border.width: 2
                border.color: FrameTheme.violet
                visible: parent.visualFocus
            }
            onClicked: Qt.openUrlExternally("https://myframe.fit")
        }
    }
    RowLayout {
        Layout.fillWidth: true
        FrameComboBox {
            id: source
            objectName: "museumSource"
            Accessible.name: "Museum"
            model: ["The Met", "Cleveland", "Chicago"]
            ToolTip.visible: hovered
            ToolTip.text: ["The Metropolitan Museum of Art", "Cleveland Museum of Art", "Art Institute of Chicago"][currentIndex]
            enabled: !page.session.busy
            onActivated: {
                field.currentIndex = 0
                if (query.text.trim() || page.collection.view === "Results") page.runSearch()
                else page.backend.museumView("Featured", page.sourceId)
            }
        }
        FrameTextField {
            id: query
            objectName: "museumQuery"
            Layout.fillWidth: true
            placeholderText: "Search " + source.currentText
            Accessible.name: placeholderText
            enabled: !page.session.busy
            onAccepted: page.runSearch()
        }
        FrameButton { text: "Search"; enabled: !page.session.busy; onClicked: page.runSearch() }
    }
    RowLayout {
        FrameButton { text: "All featured"; enabled: !page.session.busy; onClicked: { query.clear(); page.backend.museumView("Featured") } }
        FrameButton { text: "Saved"; enabled: !page.session.busy; onClicked: page.backend.museumView("Saved") }
        Item { Layout.fillWidth: true }
        FrameButton { text: "Filters…"; enabled: !page.session.busy; onClicked: filters.open() }
    }
    Label {
        Layout.fillWidth: true
        text: page.collection.view === "Results"
              ? (page.collection.query ? "Results for “" + page.collection.query + "”" : "Search results")
              : page.collection.view === "Saved" ? "Saved for later" : "Featured · " + page.collection.featuredMuseum
        font.bold: true
        elide: Text.ElideRight
        textFormat: Text.PlainText
    }
    GridView {
        id: gallery
        Layout.fillWidth: true
        Layout.fillHeight: true
        clip: true
        model: page.collection.works
        cellWidth: width / Math.max(1, Math.floor(width / 235))
        cellHeight: Math.round(cellWidth * 0.64) + 160
        ScrollBar.vertical: ScrollBar {}
        Label {
            anchors.centerIn: parent
            visible: page.collection.view === "Saved" && !gallery.count
            text: "Save a work to find it here later."
            color: FrameTheme.muted
        }
        delegate: Item {
            required property var modelData
            width: gallery.cellWidth
            height: gallery.cellHeight
            Rectangle {
                anchors.fill: parent
                anchors.margins: 4
                color: FrameTheme.panel
                border.color: FrameTheme.line
                radius: 10
                ColumnLayout {
                    anchors.fill: parent
                    anchors.margins: 9
                    spacing: 4
                    Rectangle {
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        color: FrameTheme.line
                        Image {
                            id: thumbnail
                            anchors.fill: parent
                            source: modelData.preview
                            sourceSize: Qt.size(600, 600)
                            asynchronous: true
                            fillMode: Image.PreserveAspectFit
                        }
                        Image {
                            anchors.fill: parent
                            source: modelData.placeholder || ""
                            fillMode: Image.PreserveAspectFit
                            visible: thumbnail.status !== Image.Ready
                        }
                        Label {
                            anchors.centerIn: parent
                            visible: thumbnail.status === Image.Error && !modelData.placeholder
                            text: "Preview unavailable"
                        }
                    }
                    Label {
                        Layout.fillWidth: true
                        text: modelData.title
                        font.bold: true
                        textFormat: Text.PlainText
                        elide: Text.ElideRight
                        ToolTip.visible: titleHover.hovered
                        ToolTip.text: text
                        HoverHandler { id: titleHover }
                    }
                    Label {
                        Layout.fillWidth: true
                        text: [modelData.artist, modelData.year].filter(Boolean).join(" · ")
                        font.pixelSize: 13
                        color: FrameTheme.muted
                        textFormat: Text.PlainText
                        elide: Text.ElideRight
                    }
                    Label {
                        Layout.fillWidth: true
                        text: modelData.width && modelData.height
                              ? modelData.width + " × " + modelData.height + " · " + modelData.fit
                              : modelData.sourceWidth && modelData.sourceHeight
                                ? modelData.sourceWidth + " × " + modelData.sourceHeight + " · Museum master"
                                : modelData.fit
                        font.pixelSize: 12
                        wrapMode: Text.WordWrap
                        color: FrameTheme.softInk
                    }
                    RowLayout {
                        Layout.fillWidth: true
                        FrameButton {
                            quiet: true
                            text: modelData.museum + " ↗"
                            Accessible.name: "View museum record for " + modelData.title
                            onClicked: Qt.openUrlExternally(modelData.link)
                        }
                        Item { Layout.fillWidth: true }
                        FrameButton {
                            quiet: true
                            text: modelData.saved ? "♥ Saved" : "♡ Save"
                            Accessible.name: modelData.saved ? "Remove from saved" : "Save for later"
                            onClicked: page.backend.saveMuseumWork(modelData.key)
                        }
                    }
                    RowLayout {
                        Layout.fillWidth: true
                        FrameButton {
                            primary: true
                            text: "Preview & crop…"
                            enabled: !page.session.busy
                            onClicked: page.backend.importMuseum(modelData.key)
                            Accessible.name: "Preview and crop " + modelData.title
                        }
                        Label { visible: modelData.onFrame; text: "On Frame"; font.pixelSize: 12; color: FrameTheme.green }
                    }
                }
            }
        }
    }
    RowLayout {
        Layout.fillWidth: true
        Layout.fillHeight: false
        visible: page.collection.busy || page.collection.more || page.collection.message.length > 0
        Label {
            Layout.fillWidth: true
            text: page.session.museumSending ? page.session.message : page.collection.message
            visible: text.length > 0
            textFormat: Text.PlainText
            wrapMode: Text.WordWrap
        }
        FrameButton {
            objectName: "showMuseumUpload"
            visible: !!page.collection.canShowUpload
            enabled: !!page.session.paired && !page.session.busy
            text: "Show on Frame"
            onClicked: page.backend.showMuseumUpload()
        }
        FrameButton { visible: !!page.session.museumSending; text: "Stop"; onClicked: page.backend.stop() }
        FrameButton { visible: page.collection.busy; text: "Cancel"; onClicked: page.backend.stop() }
        FrameButton {
            visible: page.collection.more && !page.collection.busy
            text: "More results"
            onClicked: page.backend.searchMuseum("", {}, true)
        }
    }
}
