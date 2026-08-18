package demo

import scala.collection.mutable

object App extends Base {
  def run(value: String): Int = {
    helper(value)
    1
  }
}

class Service extends App {
  def helper(value: String): Unit = ()
}
