// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/// @notice Minimal on-chain vote counter. Keep voter identity private by storing a one-way voter hash.
contract Voting {
    address public immutable owner;
    mapping(uint256 => mapping(uint256 => uint256)) private voteCounts;
    mapping(uint256 => mapping(bytes32 => bool)) public hasVoted;

    event VoteRecorded(uint256 indexed electionId, uint256 indexed candidateId, bytes32 indexed voterHash);

    constructor() {
        owner = msg.sender;
    }

    function vote(uint256 electionId, uint256 candidateId, bytes32 voterHash) external {
        require(!hasVoted[electionId][voterHash], "Already voted");
        hasVoted[electionId][voterHash] = true;
        voteCounts[electionId][candidateId] += 1;
        emit VoteRecorded(electionId, candidateId, voterHash);
    }

    function getVotes(uint256 electionId, uint256 candidateId) external view returns (uint256) {
        return voteCounts[electionId][candidateId];
    }
}
