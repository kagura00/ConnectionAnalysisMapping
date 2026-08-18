package demo.model

import demo.lib.Helper

typealias Id = String

interface Worker<T> {
    fun work(input: T): String
}

open class Base {
    open fun label(): String = "base"
}

data class Item(val id: Id, val label: String)

class Service<T : Any>(private val helper: Helper) : Base(), Worker<T> {
    companion object {
        fun create(helper: Helper): Service<String> = Service(helper)
    }

    constructor() : this(Helper())

    override fun work(input: T): String {
        val value: String = helper.make(input.toString())
        return value
    }

    fun process(input: String): String {
        fun local(value: String): String = value.trim()
        val action: (String) -> String = { value -> local(value) }
        return action(input)
    }

    fun String.decorate(): String = this.trim()
}

object Registry : Worker<String> {
    override fun work(input: String): String = input
    fun load(): Service<String> = Service.create(Helper())
}
