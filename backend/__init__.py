"""网页工作台的本地仿真服务。"""


class RequestError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status
