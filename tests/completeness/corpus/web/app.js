function helper() {
  return 1;
}

export function start() {
  return helper();
}

class Widget {
  render() {
    return helper();
  }
}
