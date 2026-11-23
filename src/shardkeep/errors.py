class UserError(Exception):
    """Ошибка, о которой нужно сообщить пользователю без трассировки стека."""

    def __init__(self, message, code=2):
        super().__init__(message)
        self.code = code
