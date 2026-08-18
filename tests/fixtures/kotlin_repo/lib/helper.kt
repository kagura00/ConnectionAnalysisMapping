package demo.lib

class Helper {
    fun make(value: String): String = value.trim()
    fun prepare() {}
}

fun String.decorateText(): String = trim()
