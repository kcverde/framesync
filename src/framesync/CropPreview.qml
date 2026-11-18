import QtQuick

Rectangle {
    id: viewport
    required property var photo
    property real zoom: 1
    property real panX: 0.5
    property real panY: 0.5
    readonly property real scaleFactor: Math.max(width / Math.max(1, photo.w), height / Math.max(1, photo.h)) * zoom
    readonly property real overflowX: Math.max(0, picture.width - width)
    readonly property real overflowY: Math.max(0, picture.height - height)
    readonly property bool ready: picture.status === Image.Ready
    readonly property bool failed: picture.status === Image.Error
    color: "black"
    clip: true
    Image {
        id: picture
        source: viewport.photo.url
        sourceSize: Qt.size(1536, 1536)
        asynchronous: true
        cache: false
        autoTransform: true
        width: viewport.photo.w * viewport.scaleFactor
        height: viewport.photo.h * viewport.scaleFactor
        x: -viewport.overflowX * viewport.panX
        y: -viewport.overflowY * viewport.panY
    }
}
