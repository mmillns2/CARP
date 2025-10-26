from collections import deque
from PySide6.QtCore import QObject, QMutex, QWaitCondition, QMutexLocker

class TSQueue(QObject):
    def __init__(self, parent=None):
        super().__init__(parent=parent)
        self.m_deque = deque()
        self.m_mutex = QMutex()
        self.m_cv = QWaitCondition()

    def push_back(self, obj):
        with QMutexLocker(self.m_mutex):
            self.m_deque.appendleft(obj)
            self.m_cv.wakeOne()

    def pop_front(self):
        with QMutexLocker(self.m_mutex):
            if not self.m_deque:
                return None
            return self.m_deque.pop()

    def empty(self):
        with QMutexLocker(self.m_mutex):
            return len(self.m_deque) == 0

    def wait(self):
        with QMutexLocker(self.m_mutex):
            while not self.m_deque:
                self.m_cv.wait(self.m_mutex)

    def size(self):
        with QMutexLocker(self.m_mutex):
            return len(self.m_deque)

    def clear(self):
        with QMutexLocker(self.m_mutex):
            slef.m_deque.clear()

    def notify_all(self):
        with QMutexLocker(self.m_mutex):
            self.m_cv.wakeAll()
