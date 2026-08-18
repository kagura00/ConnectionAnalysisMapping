struct Item {
    let id: Int
}

protocol Worker {
    func run()
}

class Service: Worker {
    func run() {}
}

func start() {}
