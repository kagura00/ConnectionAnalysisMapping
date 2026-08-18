class Base:
    def value(self):
        return 1


class Service(Base):
    def run(self, value):
        return self.value() + value

    async def async_run(self):
        return self.value()


def start(value):
    return Service().run(value)
