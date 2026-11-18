pragma Singleton
import QtQuick

QtObject {
    property bool dark: Qt.styleHints.colorScheme === Qt.Dark
    readonly property color canvas: dark ? "#111121" : "#f1edf7"
    readonly property color side: dark ? "#171628" : "#f9f5fb"
    readonly property color panel: dark ? "#1d1c32" : "#fffbf3"
    readonly property color softPanel: dark ? "#211e35" : "#faf6f8"
    readonly property color field: dark ? "#171628" : "#fffaf7"
    readonly property color ink: dark ? "#f2eafa" : "#2b2239"
    readonly property color softInk: dark ? "#d2c6e3" : "#51435f"
    readonly property color muted: dark ? "#af9fc3" : "#695c78"
    readonly property color line: dark ? "#312c46" : "#ded5e7"
    readonly property color strongLine: dark ? "#534763" : "#cbbbd6"
    readonly property color violet: dark ? "#d7b9ff" : "#5f4084"
    readonly property color violetBg: dark ? "#2b2143" : "#ecdefa"
    readonly property color violetLine: dark ? "#614980" : "#d0b5e9"
    readonly property color amber: "#f5bb76"
    readonly property color amberHover: dark ? "#ffd096" : "#ffc88c"
    readonly property color amberInk: "#35200d"
    readonly property color green: dark ? "#b3dcc1" : "#416652"
    readonly property color greenBg: dark ? "#233c36" : "#e1eee4"
}
