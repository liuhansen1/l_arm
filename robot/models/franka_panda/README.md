# Franka Panda 机械臂模型

本目录包含项目使用的 Panda URDF、碰撞网格和外观网格，复制自 PyBullet 3.2.7 安装包的 `pybullet_data/franka_panda/`。原始模型文件未修改，场景建立程序通过本目录的 `panda.urdf` 加载机械臂。

- 模型入口：[panda.urdf](panda.urdf)。
- 几何资源：`meshes/`，保留上游目录结构。
- 来源：[Bullet 上游模型目录](https://github.com/bulletphysics/bullet3/tree/master/examples/pybullet/gym/pybullet_data/franka_panda)。
- 模型资源沿用上游的 [Apache License 2.0](LICENSE.txt)，不适用项目原创代码的 MIT 许可证。

场景地面由 PyBullet 自带的 `plane.urdf` 提供，红、蓝、绿方块由 `robot/demo.py` 使用基本几何体生成。
