use bevy::app::PluginGroup;
use bevy::prelude::*;

#[derive(Component)]
struct Position(u64);

#[derive(Component)]
struct Velocity(u64);

fn advance(mut objects: Query<(&mut Position, &Velocity)>) {
    for (mut position, velocity) in &mut objects {
        position.0 += velocity.0;
    }
}

fn main() {
    // Keep the full default plugin graph in the linked program. Constructing the
    // group does not start its graphics/audio backends or create a window.
    drop(std::hint::black_box(DefaultPlugins.build()));
    let mut app = App::new();
    app.add_plugins(MinimalPlugins).add_systems(Update, advance);
    for value in 0..1000u64 {
        app.world_mut().spawn((Position(value), Velocity(2)));
    }
    for _ in 0..30 {
        app.update();
    }
    let mut positions = app.world_mut().query::<&Position>();
    let sum: u64 = positions.iter(app.world()).map(|position| position.0).sum();
    assert_eq!(sum, 559_500);
    assert_eq!(positions.iter(app.world()).count(), 1000);
    println!("bevy: entities=1000 updates=30 sum={sum}");
}
