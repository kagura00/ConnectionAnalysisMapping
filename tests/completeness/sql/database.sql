CREATE TABLE teams (id INT PRIMARY KEY);
CREATE TABLE users (
  id INT PRIMARY KEY,
  team_id INT REFERENCES teams(id)
);
CREATE VIEW active_users AS
  SELECT u.id FROM users u JOIN teams t ON t.id = u.team_id;
INSERT INTO users(id, team_id)
  SELECT id, 1 FROM teams;
