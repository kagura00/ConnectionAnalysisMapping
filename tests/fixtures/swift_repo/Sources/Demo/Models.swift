import Foundation

protocol Worker: Runnable {
    associatedtype Identifier
    func work(value: Int) async throws -> String
}

class Base {
    func reset() {}
}

class Service: Base, Worker {
    typealias Identifier = Int
    var helper: Helper

    init(helper: Helper) {
        self.helper = helper
    }

    func work(value: Int) async throws -> String {
        return try await helper.run(value)
    }

    func clear() {
        reset()
    }

    deinit {}
}

struct Item: Codable {
    let id: Int
}

enum State {
    case idle
    case ready(Int)
}

actor Store {
    func save() {}
}

typealias Identifier = Int
