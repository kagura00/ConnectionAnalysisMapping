CREATE TABLE teams (id INT PRIMARY KEY);
CREATE TABLE users (
  id INT PRIMARY KEY,
  team_id INT,
  CONSTRAINT fk_team FOREIGN KEY (team_id) REFERENCES teams(id)
);
CREATE VIEW active_users AS
  SELECT u.id FROM users u JOIN teams t ON t.id = u.team_id;
WITH recent AS (SELECT id FROM users)
  INSERT INTO users(id) SELECT id FROM recent;
UPDATE users SET team_id = 1 WHERE id IN (SELECT id FROM users);
DELETE FROM users WHERE id = 1;
