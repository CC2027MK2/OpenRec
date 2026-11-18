"""单实例保护：基于 QLocalServer/QLocalSocket 的命名管道。"""
from PySide6.QtCore import QObject, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket

SERVER_NAME = "OpenRec.SingleInstance.v1"


class SingleInstance(QObject):
    """首个实例监听命名管道；后续实例通知它激活窗口后自行退出。"""

    activated = Signal()

    def __init__(self, key: str = SERVER_NAME, parent=None):
        super().__init__(parent)
        self._key = key
        self._server: QLocalServer | None = None

    def try_acquire(self) -> bool:
        """返回 True 表示当前实例是唯一实例；False 表示已有实例在运行。"""
        probe = QLocalSocket()
        probe.connectToServer(self._key)
        if probe.waitForConnected(300):
            probe.write(b"show")
            probe.flush()
            probe.waitForBytesWritten(300)
            probe.disconnectFromServer()
            return False

        # 没有活动实例：清理可能残留的管道后监听
        QLocalServer.removeServer(self._key)
        self._server = QLocalServer(self)
        self._server.newConnection.connect(self._on_new_connection)
        return self._server.listen(self._key)

    def _on_new_connection(self):
        conn = self._server.nextPendingConnection()
        if not conn:
            return
        conn.readyRead.connect(lambda c=conn: self._read_and_activate(c))
        conn.disconnected.connect(conn.deleteLater)

    def _read_and_activate(self, conn: QLocalSocket):
        try:
            conn.readAll()
            conn.disconnectFromServer()
        except Exception:
            pass
        self.activated.emit()

    def close(self):
        if self._server:
            self._server.close()
            QLocalServer.removeServer(self._key)
            self._server = None
