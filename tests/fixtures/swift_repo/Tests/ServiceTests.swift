import XCTest

final class ServiceTests {
    func testService() {
        let service = Service(helper: Helper())
        service.clear()
    }
}
