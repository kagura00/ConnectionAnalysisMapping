export interface Config {
  name: string;
}

export type Identifier = string;

export class Service {
  run(value: Identifier): string {
    return value;
  }
}

export function boot(config: Config): string {
  return config.name;
}
