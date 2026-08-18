import { helper } from "./util.js";
import { Card } from "./card";

export interface Config {
  title: string;
}

export type Identifier = string | number;

export class App extends Card {
  run(value: Identifier): Config {
    helper(String(value));
    return { title: "ready" };
  }
}

export function boot(): void {
  document.querySelector("#app").addEventListener("click", helper);
  const instance = new App();
  instance.run(1);
}

export const callback = (value: Identifier) => helper(String(value));
const lazy = import("./lazy.ts");
