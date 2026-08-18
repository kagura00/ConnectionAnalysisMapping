require_relative "service"

module Demo
  def self.start
    Service.build(1)
  end
end
