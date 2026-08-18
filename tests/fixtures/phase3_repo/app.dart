import 'dart:async';

class Service extends Base {
  Future<String> run(int value) async {
    return make(value);
  }
}

void main() {
  final service = Service();
  service.run(1);
}
