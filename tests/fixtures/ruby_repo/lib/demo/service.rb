require_relative "base"
require_relative "support"
require "set"

module Demo
  class Service < Base
    include Logging
    extend FactoryMethods

    def initialize
      @helper = Helper.new
    end

    def run(value = 1, &block)
      worker = Helper.new
      worker.save(value)
      @helper.save(value)
      yield(value) if block_given?
      block.call(value) if block
      value
    end

    def self.build(value)
      new(value)
    end
  end
end
