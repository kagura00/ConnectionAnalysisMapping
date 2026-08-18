class Service {
  String run(int value) {
    return value.toString();
  }
}

void main() {
  Service().run(1);
}
