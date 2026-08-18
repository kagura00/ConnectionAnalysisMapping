package demo.app

import demo.lib.Helper
import demo.lib.decorateText as decorateText
import demo.model.Service

fun top(value: String): String = Helper().make(value)

fun useExtension(value: String): String = value.decorateText()

suspend fun asyncTop(value: String): String = top(value)

fun useService(input: String): String {
    val service: Service<String> = Service(Helper())
    return service.process(input).decorateText()
}
