//! Immutable, ordered classic SPL Token payouts. The caller supplies an action,
//! never replacement recipients or amounts. The queue account is a tombstone.
use solana_program::{
    account_info::AccountInfo,
    clock::Clock,
    entrypoint::ProgramResult,
    instruction::{AccountMeta, Instruction},
    msg,
    program::{invoke, invoke_signed},
    program_error::ProgramError,
    program_option::COption,
    program_pack::Pack,
    pubkey,
    pubkey::Pubkey,
    rent::Rent,
    sysvar::Sysvar,
};
use solana_sdk_ids::system_program;
use solana_system_interface::instruction as system_instruction;
use spl_token::state::{Account as TokenAccount, AccountState, Mint};

#[cfg(not(feature = "no-entrypoint"))]
solana_program::entrypoint!(process_instruction);

pub const ATA_PROGRAM: Pubkey = pubkey!("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL");
pub const QUEUE_SIZE: usize = 872;
pub const MAX_PAYMENTS: usize = 16;
pub const MAGIC: &[u8; 8] = b"CUPAY001";

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
#[repr(u32)]
pub enum Error {
    InvalidInstruction = 1,
    InvalidAccounts,
    Unauthorized,
    InvalidQueue,
    InvalidToken,
    InvalidProgram,
    InvalidRecipient,
    InvalidAmount,
    DuplicateRecipient,
    Overflow,
    InvalidCount,
    StaleCursor,
    Paused,
    Expired,
    NotExpired,
    Terminal,
    AlreadyExists,
    InvalidExpiry,
}
impl From<Error> for ProgramError {
    fn from(value: Error) -> Self {
        Self::Custom(value as u32)
    }
}
fn require(value: bool, error: Error) -> ProgramResult {
    if value {
        Ok(())
    } else {
        Err(error.into())
    }
}
fn key(data: &[u8], offset: usize) -> Pubkey {
    Pubkey::new_from_array(data[offset..offset + 32].try_into().unwrap())
}
fn u64_at(data: &[u8], offset: usize) -> u64 {
    u64::from_le_bytes(data[offset..offset + 8].try_into().unwrap())
}
fn i64_at(data: &[u8], offset: usize) -> i64 {
    i64::from_le_bytes(data[offset..offset + 8].try_into().unwrap())
}
fn writable(account: &AccountInfo) -> ProgramResult {
    require(account.is_writable, Error::InvalidAccounts)
}
fn signer(account: &AccountInfo) -> ProgramResult {
    require(account.is_signer, Error::Unauthorized)
}
fn wallet(account: &AccountInfo) -> ProgramResult {
    require(
        account.owner == &system_program::id() && account.data_is_empty() && !account.executable,
        Error::InvalidAccounts,
    )
}
fn program(account: &AccountInfo, expected: &Pubkey) -> ProgramResult {
    require(
        account.key == expected && account.executable && !account.is_writable && !account.is_signer,
        Error::InvalidProgram,
    )
}
pub fn queue_address(program_id: &Pubkey, owner: &Pubkey, id: &[u8]) -> (Pubkey, u8) {
    Pubkey::find_program_address(&[b"payout", owner.as_ref(), id], program_id)
}
pub fn ata_address(owner: &Pubkey, mint: &Pubkey) -> Pubkey {
    Pubkey::find_program_address(
        &[owner.as_ref(), spl_token::id().as_ref(), mint.as_ref()],
        &ATA_PROGRAM,
    )
    .0
}
fn mint(account: &AccountInfo) -> Result<Mint, ProgramError> {
    require(
        account.owner == &spl_token::id()
            && account.data_len() == Mint::LEN
            && !account.executable
            && !account.is_signer,
        Error::InvalidToken,
    )?;
    Mint::unpack(&account.try_borrow_data()?).map_err(|_| Error::InvalidToken.into())
}
fn token(
    account: &AccountInfo,
    owner: &Pubkey,
    mint: &Pubkey,
) -> Result<TokenAccount, ProgramError> {
    require(
        account.owner == &spl_token::id()
            && account.data_len() == TokenAccount::LEN
            && !account.executable
            && !account.is_signer
            && account.key == &ata_address(owner, mint),
        Error::InvalidToken,
    )?;
    let state = TokenAccount::unpack(&account.try_borrow_data()?)?;
    require(
        state.owner == *owner
            && state.mint == *mint
            && state.state == AccountState::Initialized
            && state.is_native == COption::None,
        Error::InvalidToken,
    )?;
    Ok(state)
}
fn vault(
    account: &AccountInfo,
    queue: &Pubkey,
    mint: &Pubkey,
) -> Result<TokenAccount, ProgramError> {
    let state = token(account, queue, mint)?;
    require(
        state.delegate == COption::None && state.close_authority == COption::None,
        Error::InvalidToken,
    )?;
    Ok(state)
}
fn load_queue(program_id: &Pubkey, account: &AccountInfo) -> Result<Vec<u8>, ProgramError> {
    writable(account)?;
    require(
        account.owner == program_id
            && account.data_len() == QUEUE_SIZE
            && !account.executable
            && !account.is_signer,
        Error::InvalidQueue,
    )?;
    let data = account.try_borrow_data()?.to_vec();
    require(&data[..8] == MAGIC, Error::InvalidQueue)?;
    let (expected, bump) = queue_address(program_id, &key(&data, 8), &data[104..136]);
    require(
        account.key == &expected
            && bump == data[164]
            && data[161] > 0
            && data[161] as usize <= MAX_PAYMENTS
            && data[160] <= data[161]
            && data[166] == data[160]
            && data[162] <= 1
            && data[163] <= 2
            && u64_at(&data, 152) <= u64_at(&data, 144),
        Error::InvalidQueue,
    )?;
    Ok(data)
}

/// Parses and validates only owner-approved terms; no post-execution features.
fn validate_terms(terms: &[u8], count: usize) -> Result<u64, ProgramError> {
    require(
        count > 0 && count <= MAX_PAYMENTS && terms.len() == count * 40,
        Error::InvalidCount,
    )?;
    let mut total = 0u64;
    for i in 0..count {
        let recipient = key(terms, i * 40);
        require(recipient != Pubkey::default(), Error::InvalidRecipient)?;
        for j in 0..i {
            require(key(terms, j * 40) != recipient, Error::DuplicateRecipient)?;
        }
        let amount = u64_at(terms, i * 40 + 32);
        require(amount > 0, Error::InvalidAmount)?;
        total = total.checked_add(amount).ok_or(Error::Overflow)?;
    }
    Ok(total)
}
pub fn allowed_count(count: u8, remaining: u8) -> bool {
    count > 0
        && count <= 8
        && count <= remaining
        && (matches!(count, 1 | 2 | 4 | 8) || count == remaining)
}

/// Stable classic ATA CreateIdempotent interface. All program identities were
/// checked by the caller; the ATA program validates/initializes the account.
#[allow(clippy::too_many_arguments)]
fn create_ata<'a>(
    payer: &AccountInfo<'a>,
    ata: &AccountInfo<'a>,
    owner: &AccountInfo<'a>,
    mint: &AccountInfo<'a>,
    system: &AccountInfo<'a>,
    token: &AccountInfo<'a>,
    associated: &AccountInfo<'a>,
) -> ProgramResult {
    require(
        ata.key == &ata_address(owner.key, mint.key),
        Error::InvalidToken,
    )?;
    invoke(
        &Instruction {
            program_id: ATA_PROGRAM,
            accounts: vec![
                AccountMeta::new(*payer.key, true),
                AccountMeta::new(*ata.key, false),
                AccountMeta::new_readonly(*owner.key, false),
                AccountMeta::new_readonly(*mint.key, false),
                AccountMeta::new_readonly(system_program::id(), false),
                AccountMeta::new_readonly(spl_token::id(), false),
            ],
            data: vec![1],
        },
        &[
            payer.clone(),
            ata.clone(),
            owner.clone(),
            mint.clone(),
            system.clone(),
            token.clone(),
            associated.clone(),
        ],
    )
}
#[allow(clippy::too_many_arguments)]
fn transfer<'a>(
    source: &AccountInfo<'a>,
    mint: &AccountInfo<'a>,
    destination: &AccountInfo<'a>,
    authority: &AccountInfo<'a>,
    token_program: &AccountInfo<'a>,
    amount: u64,
    decimals: u8,
    seeds: &[&[&[u8]]],
) -> ProgramResult {
    let instruction = spl_token::instruction::transfer_checked(
        &spl_token::id(),
        source.key,
        mint.key,
        destination.key,
        authority.key,
        &[],
        amount,
        decimals,
    )?;
    invoke_signed(
        &instruction,
        &[
            source.clone(),
            mint.clone(),
            destination.clone(),
            authority.clone(),
            token_program.clone(),
        ],
        seeds,
    )
}

fn create_queue(program_id: &Pubkey, accounts: &[AccountInfo], data: &[u8]) -> ProgramResult {
    require(
        accounts.len() == 8 && data.len() >= 73,
        Error::InvalidInstruction,
    )?;
    let count = data[72] as usize;
    let total = validate_terms(&data[73..], count)?;
    let expiry = i64_at(data, 32);
    require(expiry > Clock::get()?.unix_timestamp, Error::InvalidExpiry)?;
    let executor = key(data, 40);
    require(executor != Pubkey::default(), Error::Unauthorized)?;
    let [owner, queue, vault_account, mint_account, source, token_program, associated, system] =
        accounts
    else {
        unreachable!()
    };
    signer(owner)?;
    writable(owner)?;
    wallet(owner)?;
    writable(queue)?;
    writable(vault_account)?;
    writable(source)?;
    program(token_program, &spl_token::id())?;
    program(associated, &ATA_PROGRAM)?;
    program(system, &system_program::id())?;
    let mint_state = mint(mint_account)?;
    require(
        mint_account.key != &spl_token::native_mint::id(),
        Error::InvalidToken,
    )?;
    token(source, owner.key, mint_account.key)?;
    let (expected, bump) = queue_address(program_id, owner.key, &data[..32]);
    require(
        queue.key == &expected && !queue.is_signer,
        Error::InvalidQueue,
    )?;
    require(
        queue.owner == &system_program::id() && queue.data_is_empty(),
        Error::AlreadyExists,
    )?;
    let bump_seed = [bump];
    let seeds: &[&[u8]] = &[b"payout", owner.key.as_ref(), &data[..32], &bump_seed];
    let rent = Rent::get()?.minimum_balance(QUEUE_SIZE);
    // Accept an otherwise empty prefunded PDA so unsolicited SOL cannot squat it.
    if queue.lamports() == 0 {
        invoke_signed(
            &system_instruction::create_account(
                owner.key,
                queue.key,
                rent,
                QUEUE_SIZE as u64,
                program_id,
            ),
            &[owner.clone(), queue.clone(), system.clone()],
            &[seeds],
        )?;
    } else {
        let needed = rent.saturating_sub(queue.lamports());
        if needed > 0 {
            invoke(
                &system_instruction::transfer(owner.key, queue.key, needed),
                &[owner.clone(), queue.clone(), system.clone()],
            )?;
        }
        invoke_signed(
            &system_instruction::allocate(queue.key, QUEUE_SIZE as u64),
            &[queue.clone(), system.clone()],
            &[seeds],
        )?;
        invoke_signed(
            &system_instruction::assign(queue.key, program_id),
            &[queue.clone(), system.clone()],
            &[seeds],
        )?;
    }
    create_ata(
        owner,
        vault_account,
        queue,
        mint_account,
        system,
        token_program,
        associated,
    )?;
    vault(vault_account, queue.key, mint_account.key)?;
    transfer(
        source,
        mint_account,
        vault_account,
        owner,
        token_program,
        total,
        mint_state.decimals,
        &[],
    )?;
    let mut state = queue.try_borrow_mut_data()?;
    state.fill(0);
    state[..8].copy_from_slice(MAGIC);
    state[8..40].copy_from_slice(owner.key.as_ref());
    state[40..72].copy_from_slice(executor.as_ref());
    state[72..104].copy_from_slice(mint_account.key.as_ref());
    state[104..136].copy_from_slice(&data[..32]);
    state[136..144].copy_from_slice(&expiry.to_le_bytes());
    state[144..152].copy_from_slice(&total.to_le_bytes());
    state[161] = count as u8;
    state[164] = bump;
    state[165] = mint_state.decimals;
    state[232..232 + count * 40].copy_from_slice(&data[73..]);
    msg!("payout:create count={} total={}", count, total);
    Ok(())
}

fn execute_batch(program_id: &Pubkey, accounts: &[AccountInfo], data: &[u8]) -> ProgramResult {
    require(
        data.len() == 66 && accounts.len() >= 7,
        Error::InvalidInstruction,
    )?;
    let expected_cursor = data[0];
    let count = data[1];
    require(
        accounts.len() == 7 + count as usize * 2,
        Error::InvalidAccounts,
    )?;
    let executor = &accounts[0];
    let queue = &accounts[1];
    let vault_account = &accounts[2];
    let mint_account = &accounts[3];
    let token_program = &accounts[4];
    let associated = &accounts[5];
    let system = &accounts[6];
    signer(executor)?;
    writable(executor)?;
    wallet(executor)?;
    writable(vault_account)?;
    program(token_program, &spl_token::id())?;
    program(associated, &ATA_PROGRAM)?;
    program(system, &system_program::id())?;
    let mut state = load_queue(program_id, queue)?;
    require(executor.key == &key(&state, 40), Error::Unauthorized)?;
    require(state[163] == 0, Error::Terminal)?;
    require(state[162] == 0, Error::Paused)?;
    require(
        Clock::get()?.unix_timestamp < i64_at(&state, 136),
        Error::Expired,
    )?;
    require(expected_cursor == state[160], Error::StaleCursor)?;
    require(
        allowed_count(count, state[161] - state[160]),
        Error::InvalidCount,
    )?;
    require(mint_account.key == &key(&state, 72), Error::InvalidToken)?;
    require(
        mint(mint_account)?.decimals == state[165],
        Error::InvalidToken,
    )?;
    vault(vault_account, queue.key, mint_account.key)?;
    let owner = key(&state, 8);
    let id: [u8; 32] = state[104..136].try_into().unwrap();
    let bump = [state[164]];
    let seeds: &[&[u8]] = &[b"payout", owner.as_ref(), &id, &bump];
    let mut total_paid = u64_at(&state, 152);
    for i in 0..count as usize {
        let offset = 232 + (expected_cursor as usize + i) * 40;
        let recipient = &accounts[7 + i * 2];
        let destination = &accounts[8 + i * 2];
        require(
            recipient.key == &key(&state, offset),
            Error::InvalidRecipient,
        )?;
        wallet(recipient)?;
        writable(destination)?;
        require(
            destination.key == &ata_address(recipient.key, mint_account.key),
            Error::InvalidRecipient,
        )?;
        // Skip an unnecessary ATA CPI for existing accounts: missing ATAs are
        // the actual state-dependent workload learned by the reference app.
        if destination.owner == &system_program::id() && destination.data_is_empty() {
            create_ata(
                executor,
                destination,
                recipient,
                mint_account,
                system,
                token_program,
                associated,
            )?;
        }
        token(destination, recipient.key, mint_account.key)?;
        let amount = u64_at(&state, offset + 32);
        transfer(
            vault_account,
            mint_account,
            destination,
            queue,
            token_program,
            amount,
            state[165],
            &[seeds],
        )?;
        total_paid = total_paid.checked_add(amount).ok_or(Error::Overflow)?;
    }
    require(total_paid <= u64_at(&state, 144), Error::Overflow)?;
    state[160] = expected_cursor.checked_add(count).ok_or(Error::Overflow)?;
    state[166] = state[160];
    state[152..160].copy_from_slice(&total_paid.to_le_bytes());
    state[168..200].copy_from_slice(&data[2..34]);
    state[200..232].copy_from_slice(&data[34..66]);
    if state[160] == state[161] {
        state[163] = 1;
    }
    queue.try_borrow_mut_data()?.copy_from_slice(&state);
    msg!(
        "payout:execute cursor={} count={} total_paid={}",
        expected_cursor,
        count,
        total_paid
    );
    Ok(())
}

fn set_paused(program_id: &Pubkey, accounts: &[AccountInfo], data: &[u8]) -> ProgramResult {
    require(
        accounts.len() == 2 && data.len() == 1 && data[0] <= 1,
        Error::InvalidInstruction,
    )?;
    signer(&accounts[0])?;
    let mut state = load_queue(program_id, &accounts[1])?;
    require(accounts[0].key == &key(&state, 8), Error::Unauthorized)?;
    require(state[163] == 0, Error::Terminal)?;
    state[162] = data[0];
    accounts[1].try_borrow_mut_data()?.copy_from_slice(&state);
    Ok(())
}
fn refund(program_id: &Pubkey, accounts: &[AccountInfo], data: &[u8]) -> ProgramResult {
    require(
        accounts.len() == 6 && data.is_empty(),
        Error::InvalidInstruction,
    )?;
    let [owner, queue, vault_account, mint_account, destination, token_program] = accounts else {
        unreachable!()
    };
    signer(owner)?;
    writable(vault_account)?;
    writable(destination)?;
    program(token_program, &spl_token::id())?;
    let mut state = load_queue(program_id, queue)?;
    require(owner.key == &key(&state, 8), Error::Unauthorized)?;
    require(state[163] == 0, Error::Terminal)?;
    require(
        Clock::get()?.unix_timestamp >= i64_at(&state, 136),
        Error::NotExpired,
    )?;
    require(mint_account.key == &key(&state, 72), Error::InvalidToken)?;
    require(
        mint(mint_account)?.decimals == state[165],
        Error::InvalidToken,
    )?;
    let vault_state = vault(vault_account, queue.key, mint_account.key)?;
    token(destination, owner.key, mint_account.key)?;
    let id: [u8; 32] = state[104..136].try_into().unwrap();
    let bump = [state[164]];
    let seeds: &[&[u8]] = &[b"payout", owner.key.as_ref(), &id, &bump];
    // Include unsolicited deposits; only the approving owner can receive them.
    transfer(
        vault_account,
        mint_account,
        destination,
        queue,
        token_program,
        vault_state.amount,
        state[165],
        &[seeds],
    )?;
    state[163] = 2;
    state[162] = 1;
    queue.try_borrow_mut_data()?.copy_from_slice(&state);
    msg!("payout:refund amount={}", vault_state.amount);
    Ok(())
}
pub fn process_instruction(
    program_id: &Pubkey,
    accounts: &[AccountInfo],
    data: &[u8],
) -> ProgramResult {
    // Stable eight-byte discriminator matches CU Pilot's generic custom-program
    // pattern extraction. Cursor, count and audit IDs remain bound arguments.
    require(
        data.len() >= 8 && data[1..8] == [0; 7],
        Error::InvalidInstruction,
    )?;
    let args = &data[8..];
    match data[0] {
        0 => create_queue(program_id, accounts, args),
        1 => execute_batch(program_id, accounts, args),
        2 => set_paused(program_id, accounts, args),
        3 => refund(program_id, accounts, args),
        _ => Err(Error::InvalidInstruction.into()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn counts_only_allow_menu_and_complete_tail() {
        for remaining in 1..=16 {
            for count in 0..=17 {
                let expected = count > 0
                    && count <= remaining
                    && count <= 8
                    && ([1, 2, 4, 8].contains(&count) || count == remaining);
                assert_eq!(allowed_count(count, remaining), expected);
            }
        }
        assert!(!allowed_count(3, 16));
        assert!(allowed_count(3, 3));
    }
    #[test]
    fn invalid_wire_never_panics() {
        let id = Pubkey::new_unique();
        for length in 0..80 {
            for tag in 0..5 {
                let mut data = vec![0; length];
                if !data.is_empty() {
                    data[0] = tag;
                }
                assert!(process_instruction(&id, &[], &data).is_err());
            }
        }
    }
    #[test]
    fn zero_count_and_bad_lengths_rejected() {
        assert_eq!(validate_terms(&[], 0), Err(Error::InvalidCount.into()));
        assert_eq!(validate_terms(&[0; 40], 2), Err(Error::InvalidCount.into()));
        assert_eq!(validate_terms(&[], 17), Err(Error::InvalidCount.into()));
    }
    #[test]
    fn duplicates_zero_and_overflow_rejected() {
        // Find deterministic on-curve public keys, without a signing identity.
        let mut data = Vec::new();
        for byte in 1..=255u8 {
            let candidate = Pubkey::new_from_array([byte; 32]);
            if candidate.is_on_curve() {
                data.extend_from_slice(candidate.as_ref());
                data.extend_from_slice(&1u64.to_le_bytes());
            }
            if data.len() == 80 {
                break;
            }
        }
        assert_eq!(validate_terms(&data, 2), Ok(2));
        let mut duplicate = data[..40].to_vec();
        duplicate.extend_from_slice(&data[..40]);
        assert_eq!(
            validate_terms(&duplicate, 2),
            Err(Error::DuplicateRecipient.into())
        );
        data[32..40].copy_from_slice(&0u64.to_le_bytes());
        assert_eq!(validate_terms(&data, 2), Err(Error::InvalidAmount.into()));
        data[32..40].copy_from_slice(&u64::MAX.to_le_bytes());
        assert_eq!(validate_terms(&data, 2), Err(Error::Overflow.into()));
    }
}
