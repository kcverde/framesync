import QtQuick
import QtQuick.Controls.Basic
import QtQuick.Layouts

FrameDialog {
    id: dialog
    required property var backend
    readonly property var settings: backend ? backend.frameSettings : null
    readonly property var values: settings ? settings.state : ({})
    readonly property var slide: values.slideshow || ({})
    readonly property bool busy: !backend || backend.state.busy
    property string section: "display"

    function selectSection(name) {
        section = name
        settings.load(name)
    }
    function showSettings() {
        open()
        selectSection("display")
    }
    function collectionIds() {
        let ids = ["MY-C0002", "MY-C0004"]
        if (slide.category && !ids.includes(slide.category)) ids.push(slide.category)
        return ids
    }

    parent: Overlay.overlay
    anchors.centerIn: parent
    modal: true
    width: Math.min(500, parent.width - 32)
    title: "Frame settings"
    standardButtons: Dialog.Close
    contentItem: ColumnLayout {
        spacing: 10
        RowLayout {
            FrameButton { text: "Picture and sensors"; checked: dialog.section === "display"; enabled: !dialog.busy; onClicked: dialog.selectSection("display") }
            FrameButton { text: "Slideshow"; checked: dialog.section === "slideshow"; enabled: !dialog.busy; onClicked: dialog.selectSection("slideshow") }
        }
        Repeater {
            model: dialog.section === "display" ? (dialog.values.display || []) : []
            delegate: RowLayout {
                required property var modelData
                Layout.fillWidth: true
                Label { text: modelData.label; Layout.fillWidth: true; wrapMode: Text.WordWrap }
                Label { visible: !modelData.choices.length; text: modelData.value }
                FrameComboBox {
                    visible: !!modelData.choices.length
                    enabled: !dialog.busy
                    Layout.preferredWidth: 100
                    model: modelData.choices
                    currentIndex: modelData.choices.indexOf(modelData.value)
                    Accessible.name: modelData.label
                    onActivated: {
                        const requested = currentText
                        // Keep displaying the confirmed value until the TV replies.
                        currentIndex = Qt.binding(() => modelData.choices.indexOf(modelData.value))
                        dialog.settings.setDisplay(modelData.key, requested)
                    }
                }
            }
        }
        Label {
            visible: dialog.section === "display" && dialog.values.display !== undefined && !dialog.values.display.length
            text: "This Frame reported no adjustable picture or sensor settings."
            wrapMode: Text.WordWrap
            Layout.fillWidth: true
        }
        GridLayout {
            columns: 2
            Layout.fillWidth: true
            visible: dialog.section === "slideshow" && !!dialog.slide.api
            enabled: !dialog.busy
            Label { text: "Interval (minutes)" }
            SpinBox {
                id: duration
                objectName: "slideshowDuration"
                editable: true
                from: 0; to: 2147483647
                value: dialog.slide.minutes || 0
                Accessible.name: "Slideshow interval in minutes, zero for off"
            }
            Label { text: "Collection" }
            FrameComboBox {
                id: collection
                readonly property var ids: dialog.collectionIds()
                model: ids.map(id => id === "MY-C0002" ? "My photos" : id === "MY-C0004" ? "Favourites" : id)
                currentIndex: Math.max(0, ids.indexOf(dialog.slide.category))
                Accessible.name: "Slideshow collection"
            }
            Label { text: "Playback" }
            CheckBox { id: shuffle; text: "Shuffle"; checked: !!dialog.slide.shuffle }
            Label { text: duration.value === 0 ? "Slideshow off" : "" }
            FrameButton {
                text: "Apply slideshow"
                onClicked: dialog.settings.setSlideshow(duration.value, shuffle.checked, collection.ids[collection.currentIndex])
            }
        }
        Label { Layout.fillWidth: true; textFormat: Text.PlainText; text: dialog.backend ? dialog.backend.state.message : ""; wrapMode: Text.WordWrap }
    }
}
