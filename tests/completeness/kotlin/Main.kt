package demo

interface Worker {
    fun run()
}

class Service : Worker {
    override fun run() {}
}

fun start() {
    Service().run()
}
