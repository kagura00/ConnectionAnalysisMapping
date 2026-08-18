final class Helper {
    func run(_ value: Int) async throws -> String {
        return String(value)
    }
}

extension Service {
    func decorated(value: Int) -> String {
        return "\(value)"
    }
}

func makeService(helper: Helper) -> Service {
    return Service(helper: helper)
}

func topLevel(service: Service) {
    let closure = { (value: Int) in value + 1 }
    _ = closure(1)
    service.decorated(value: 1)
    _ = makeService(helper: Helper())
}
