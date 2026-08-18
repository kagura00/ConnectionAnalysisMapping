module Demo
  module Logging
    def log(value)
      value
    end
  end

  module FactoryMethods
    def build(value)
      new(value)
    end
  end

  class Helper
    def save(value)
      value
    end
  end

  class Tracing
  end
end
