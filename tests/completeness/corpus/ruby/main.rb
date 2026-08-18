module Demo
  class Service
    def run
      1
    end
  end

  def self.start
    Service.new.run
  end
end
