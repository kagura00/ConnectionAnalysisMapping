module Demo::Inline
  class Item
    def make
      Demo::Helper.new
    end

    def shorthand = Demo::Helper.new
  end
end
