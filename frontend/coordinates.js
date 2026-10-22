export function roundedPosition(position) {
  if (!Array.isArray(position) || position.length !== 3 || !position.every(Number.isFinite)) {
    throw new Error("坐标必须包含三个有效数字。");
  }
  return position.map(value => Number(value.toFixed(3)) + 0);
}

export function formatCoordinateInput(input) {
  if (input.value.trim() && Number.isFinite(input.valueAsNumber)) {
    input.value = Number(input.valueAsNumber.toFixed(3)).toFixed(3);
  }
}

function multiply(matrix, value) {
  return [0, 1, 2, 3].map(row => value.reduce((sum, v, column) => sum + matrix[column * 4 + row] * v, 0));
}

export function projectPosition(position, camera) {
  if (!camera) return null;
  const clip = multiply(camera.projection_matrix, multiply(camera.view_matrix, [...position, 1]));
  if (clip[3] <= 0) return null;
  const point = [(clip[0] / clip[3] + 1) / 2, (1 - clip[1] / clip[3]) / 2];
  return point.every(v => v >= 0 && v < 1) ? point : null;
}

function inverse(matrix) {
  const rows = [0, 1, 2, 3].map(row => [
    ...[0, 1, 2, 3].map(column => matrix[column * 4 + row]),
    ...[0, 1, 2, 3].map(column => Number(column === row)),
  ]);
  for (let column = 0; column < 4; column++) {
    let pivot = column;
    for (let row = column + 1; row < 4; row++) if (Math.abs(rows[row][column]) > Math.abs(rows[pivot][column])) pivot = row;
    [rows[pivot], rows[column]] = [rows[column], rows[pivot]];
    const scale = rows[column][column];
    if (Math.abs(scale) < 1e-12) throw new Error("相机矩阵不可逆。");
    rows[column] = rows[column].map(value => value / scale);
    for (let row = 0; row < 4; row++) {
      if (row === column) continue;
      const factor = rows[row][column];
      rows[row] = rows[row].map((value, index) => value - factor * rows[column][index]);
    }
  }
  return [0, 1, 2, 3].flatMap(column => rows.map(row => row[column + 4]));
}

export function pointOnGround(x, y, camera) {
  const combined = [0, 1, 2, 3].flatMap(column => multiply(camera.projection_matrix, camera.view_matrix.slice(column * 4, column * 4 + 4)));
  const unproject = inverse(combined);
  const points = [-1, 1].map(z => {
    const world = multiply(unproject, [2 * x - 1, 1 - 2 * y, z, 1]);
    return world.slice(0, 3).map(value => value / world[3]);
  });
  const [near, far] = points;
  const distance = far[2] - near[2];
  if (Math.abs(distance) < 1e-10 || -near[2] / distance < 0) throw new Error("该视线与地面不相交。");
  return roundedPosition(near.map((value, i) => i === 2 ? 0 : value + (far[i] - value) * (-near[2] / distance)));
}
