use rand::RngCore;
use rand_chacha::ChaChaRng;

use engine_shared::gas::Gas;

use super::Error;
use crate::engine_state::execution_result::ExecutionResult;
use crate::execution::create_rng;

fn on_fail_charge_test_helper<T>(
    f: impl Fn() -> Result<T, Error>,
    success_cost: Gas,
    error_cost: Gas,
) -> ExecutionResult {
    let _result = on_fail_charge!(f(), error_cost);
    ExecutionResult::Success {
        effect: Default::default(),
        cost: success_cost,
    }
}

#[test]
fn on_fail_charge_ok_test() {
    let val = Gas::from_u64(123);
    match on_fail_charge_test_helper(|| Ok(()), val, Gas::from_u64(456)) {
        ExecutionResult::Success { cost, .. } => assert_eq!(cost, val),
        ExecutionResult::Failure { .. } => panic!("Should be success"),
    }
}
#[test]
fn on_fail_charge_err_laziness_test() {
    let error_cost = Gas::from_u64(456);
    match on_fail_charge_test_helper(
        || Err(Error::GasLimit) as Result<(), _>,
        Gas::from_u64(123),
        error_cost,
    ) {
        ExecutionResult::Success { .. } => panic!("Should fail"),
        ExecutionResult::Failure { cost, .. } => assert_eq!(cost, error_cost),
    }
}
#[test]
fn on_fail_charge_with_action() {
    use crate::engine_state::execution_effect::ExecutionEffect;
    use crate::engine_state::op::Op;
    use contract_ffi::key::Key;
    use engine_shared::transform::Transform;
    let f = || {
        let input: Result<(), Error> = Err(Error::GasLimit);
        on_fail_charge!(input, Gas::from_u64(456), {
            let mut effect = ExecutionEffect::default();

            effect.ops.insert(Key::Hash([42u8; 32]), Op::Read);
            effect
                .transforms
                .insert(Key::Hash([42u8; 32]), Transform::Identity);

            effect
        });
        ExecutionResult::Success {
            effect: Default::default(),
            cost: Gas::default(),
        }
    };
    match f() {
        ExecutionResult::Success { .. } => panic!("Should fail"),
        ExecutionResult::Failure { cost, effect, .. } => {
            assert_eq!(cost, Gas::from_u64(456));
            // Check if the containers are non-empty
            assert_eq!(effect.ops.len(), 1);
            assert_eq!(effect.transforms.len(), 1);
        }
    }
}

fn gen_random(rng: &mut ChaChaRng) -> [u8; 32] {
    let mut buff = [0u8; 32];
    rng.fill_bytes(&mut buff);
    buff
}

#[test]
fn should_generate_different_numbers_for_different_seeds() {
    let mut rng_a = create_rng([1u8; 32], contract_ffi::execution::Phase::Session);
    let mut rng_b = create_rng([2u8; 32], contract_ffi::execution::Phase::Session);
    let random_a = gen_random(&mut rng_a);
    let random_b = gen_random(&mut rng_b);

    assert_ne!(random_a, random_b)
}

#[test]
fn should_generate_same_numbers_for_same_seed() {
    let mut rng_a = create_rng([1u8; 32], contract_ffi::execution::Phase::Session);
    let mut rng_b = create_rng([1u8; 32], contract_ffi::execution::Phase::Session);
    let random_a = gen_random(&mut rng_a);
    let random_b = gen_random(&mut rng_b);

    assert_eq!(random_a, random_b)
}

#[test]
fn should_not_generate_same_numbers_for_different_phase() {
    let deploy_hash = [1u8; 32];
    let mut rng_a = create_rng(deploy_hash, contract_ffi::execution::Phase::Payment);
    let mut rng_b = create_rng(deploy_hash, contract_ffi::execution::Phase::Session);
    let random_a = gen_random(&mut rng_a);
    let random_b = gen_random(&mut rng_b);

    assert_ne!(
        random_a, random_b,
        "different phase should have different output"
    );

    let mut rng_c = create_rng(deploy_hash, contract_ffi::execution::Phase::FinalizePayment);
    let random_c = gen_random(&mut rng_c);

    assert_ne!(
        random_a, random_c,
        "different phase should have different output"
    );
}
